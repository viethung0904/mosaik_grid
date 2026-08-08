"""
scenario_complete.py -- one-shot pipeline: CIM XML -> Neo4j -> topology preview
-> adaptive NR co-simulation.

Merges the logic of three scripts into a single, independently runnable file
(no subprocess calls, no imports from the other three -- Option B from the
scenario_complete design discussion):

  1. database/add_data_slack_detection.py  (CIM XML -> Neo4j)
  2. src/connect.py                        (Neo4j -> mosaik world.connect() preview)
  3. src/scenario_adaptive.py              (Neo4j -> mosaik NR power-flow run)

Ingestion validates every element against the simulators actually available in
src/simulator/ (see _CIM_TAG_TO_SIMULATOR_CATEGORY / _discover_available_simulator_categories)
and raises *before* touching Neo4j if it finds equipment with no matching FMU --
so a CIM file that can't be fully instantiated never reaches the simulation
stage, and existing Neo4j data is left untouched.

Usage:
    python scenario_complete.py <cim_file_or_folder>

    cim_file_or_folder : a single combined CIM .xml file, OR a folder
                         containing the CGMES *_Equipment.xml / *_Topology.xml
                         / *_StateVariables.xml triplet. Relative paths are
                         resolved against the project root, so this runs the
                         same regardless of the caller's working directory.
"""
import glob
import json
import os
import shutil
import sys
import webbrowser
from xml.etree.ElementTree import parse as et_parse

from dotenv import load_dotenv
from neo4j import GraphDatabase
import mosaik
import plotly.graph_objects as go
from plotly.subplots import make_subplots

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, os.path.join(SCRIPT_DIR, 'simulator'))
sys.path.insert(0, SCRIPT_DIR)
# database/ on path so visualize can be imported for graph auto-regen
sys.path.insert(0, os.path.join(PROJECT_DIR, 'database'))

from live_server import start_live_server, generate_live_dashboard  # noqa: E402  (needs sys.path above)

FMU_DIR = os.path.join(PROJECT_DIR, 'fmus')

load_dotenv(os.path.join(PROJECT_DIR, '.env'))
NEO4J_URI = os.getenv("NEO4J_URI")
NEO4J_USERNAME = os.getenv("NEO4J_USERNAME")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD")
NEO4J_DATABASE = os.getenv("NEO4J_DATABASE")


# ============================================================================
# Step 1 -- CIM ingestion (from database/add_data_slack_detection.py)
# ============================================================================

RDF_NS = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
CIM_NS = "http://iec.ch/TC57/2012/CIM-schema-cim16#"

SIMULATOR_DIR = os.path.join(SCRIPT_DIR, "simulator")

def _find_cgmes_files(folder):
    """
    Auto-detect EQ / TP / SV file paths inside *folder* by matching the
    CGMES suffix patterns (_Equipment, _Topology, _StateVariables).
    Raises FileNotFoundError if any file is missing.
    """
    def _find(suffix):
        matches = (
            glob.glob(os.path.join(folder, f"*{suffix}.xml")) +
            glob.glob(os.path.join(folder, f"*{suffix[1:]}.xml"))  # without leading _
        )
        # also try short NEPLAN suffixes (_EQ, _TP, _SV)
        short = {"_Equipment": "_EQ", "_Topology": "_TP", "_StateVariables": "_SV"}
        if suffix in short:
            matches += glob.glob(os.path.join(folder, f"*{short[suffix]}.xml"))
        if not matches:
            raise FileNotFoundError(f"No file matching *{suffix}.xml in {folder!r}")
        return matches[0]

    return _find("_Equipment"), _find("_Topology"), _find("_StateVariables")

# Known CIM conducting-equipment tags, mapped to the "category" name a live
# simulator would use (matching src/simulator/<category>*_simulator.py, as
# wired into scenario_adaptive.py's sim_config). Includes both the equipment
# this pipeline already models AND known switching/compensation equipment
# that has no simulator today — so its absence is reported by name instead
# of silently dropped. Any tag NOT in this map (naming, ratings, operational
# limits, diagram/state-variable profile tags, etc.) is not conducting
# equipment and is always ignored, regardless of simulator availability.
_CIM_TAG_TO_SIMULATOR_CATEGORY = {
    "Substation":         "substation",
    "ACLineSegment":      "line",
    "PowerTransformer":   "transformer",
    "PowerTransformerEnd": "transformer",
    "EnergyConsumer":     "load",
    "PhotovoltaicUnit":   "pv",
    "BatteryUnit":        "battery",
    "BatteryStorage":     "battery",
    "Breaker":               "breaker",
    "Disconnector":          "disconnector",
    "LoadBreakSwitch":       "load_break_switch",
    "Jumper":                "jumper",
    "Fuse":                  "fuse",
    "GroundDisconnector":    "ground_disconnector",
    "ProtectedSwitch":       "protected_switch",
    "Switch":                "switch",
    "ShuntCompensator":            "shunt_compensator",
    "LinearShuntCompensator":      "shunt_compensator",
    "NonlinearShuntCompensator":   "shunt_compensator",
    "StaticVarCompensator":        "static_var_compensator",
    "SeriesCompensator":           "series_compensator",
    "EquivalentBranch":            "equivalent_branch",
    "AsynchronousMachine":         "asynchronous_machine",
}


def _discover_available_simulator_categories():
    """
    Derive the set of grid-element categories with a live FMU/simulator by
    scanning src/simulator/*_simulator.py — the same modules scenario_adaptive.py
    wires into sim_config. Adding or removing a simulator file changes what
    parse_equipment() accepts without editing this script.
    """
    categories = set()
    for path in glob.glob(os.path.join(SIMULATOR_DIR, "*_simulator.py")):
        stem = os.path.basename(path)[: -len("_simulator.py")]
        stem = stem.replace("constant_", "").replace("_nr", "").replace("_branch", "")
        categories.add(stem)
    return categories


# The FMU each simulator category loads (from scenario_adaptive.py's world.start
# calls). A category is only fully runnable if BOTH its simulator .py and this
# .fmu exist; parse_equipment() checks the FMU too, so a missing .fmu is caught
# up front instead of blowing up later at world.start().
_CATEGORY_TO_FMU = {
    "substation":  "SubstationNR.fmu",
    "line":        "ACLineSegment.fmu",
    "transformer": "TransformerBranch.fmu",
    "load":        "Load.fmu",
    "battery":     "Battery_Simulink_fmi3.fmu",
    "pv":          "PV_Python_fmi2.fmu",
}


def _fmu_exists(category):
    """True if the FMU a simulator category loads is present in fmus/."""
    fmu = _CATEGORY_TO_FMU.get(category)
    return bool(fmu) and os.path.isfile(os.path.join(FMU_DIR, fmu))


def _rdf_id(elem):
    """Return the rdf:ID or rdf:about value of an element, stripped of '#'."""
    val = elem.get(f"{{{RDF_NS}}}ID") or elem.get(f"{{{RDF_NS}}}about", "")
    return val.lstrip("#")


def _resource(elem, child_local):
    """Return the rdf:resource of a CIM child element, stripped of '#'."""
    child = elem.find(f"{{{CIM_NS}}}{child_local}")
    if child is None:
        return None
    return child.get(f"{{{RDF_NS}}}resource", "").lstrip("#")


def _text(elem, child_local):
    return elem.findtext(f"{{{CIM_NS}}}{child_local}")


def _float(elem, child_local):
    val = _text(elem, child_local)
    return float(val) if val is not None else None

def parse_equipment():
    """
    Parse Equipment XML and return:
      substations        : dict  id -> property dict
      lines              : dict  id -> property dict
      terminals          : dict  id -> {equipment_id, seq}
      transformers       : dict  id -> property dict (aggregated from both ends)
      transformer_ends   : dict  end_id -> {transformer_id, terminal_id, end_number, **params}
      loads              : dict  id -> property dict
      vl_to_sub          : dict  voltage_level_id -> substation rdf_id
    """
    root = et_parse(EQ_FILE).getroot()

    base_voltages = {}      # id -> nominal_voltage_kv (float)
    voltage_levels = {}     # id -> {substation_id, base_voltage_id}
    regions = {}            # id -> region_name
    substations = {}        # id -> props
    lines = {}              # id -> props
    terminals = {}          # id -> {equipment_id, seq}
    transformers = {}       # id -> props (filled after end parsing)
    transformer_ends = {}   # end_id -> props
    loads = {}              # id -> props
    batteries = {}          # id -> props  (PowerElectronicsConnection linked to BatteryUnit)
    pv_units = {}           # id -> props  (PowerElectronicsConnection linked to PhotovoltaicUnit)
    sync_machines = {}      # id -> name  (voltage-regulating generators / PV buses)
    pec_raw = {}            # pec_id -> raw PEC attributes (intermediate)
    pv_unit_raw = {}        # unit_id -> {name, max_p, min_p}
    bat_unit_raw = {}       # unit_id -> {name, rated_e, max_p, min_p}
    battery_storage_raw = {}    # rdf_id -> raw ENTSO-E BatteryStorage attributes (intermediate)
    electrical_capacity_raw = {}  # rdf_id -> {value, unit}  (linked from BatteryStorage.ElectricalCapacity)

    unrecognized_elements = []  # (tag, name, reason) for equipment we can't simulate
    _available_categories = _discover_available_simulator_categories()

    for elem in root:
        tag = elem.tag
        rid = _rdf_id(elem)
        _local_tag = tag.split("}")[-1] if "}" in tag else tag
        _category = _CIM_TAG_TO_SIMULATOR_CATEGORY.get(_local_tag)
        if _category is not None:
            # Local name matches a known CIM equipment class. Confirm it's really
            # a CIM element (right namespace), then that it has BOTH a simulator
            # and its FMU.
            _ns = tag[1:tag.index("}")] if tag.startswith("{") else ""
            _reason = None
            if _ns != CIM_NS:
                _reason = f"not a CIM element (namespace: {_ns or 'none'})"
            elif _category not in _available_categories:
                _reason = "no simulator found"
            elif not _fmu_exists(_category):
                _reason = f"no FMU found ({_CATEGORY_TO_FMU.get(_category, '?')})"
            if _reason:
                _name = _text(elem, "IdentifiedObject.name") or rid or "<unnamed>"
                unrecognized_elements.append((_local_tag, _name, _reason))

        if tag == f"{{{CIM_NS}}}BaseVoltage":
            nv = _text(elem, "BaseVoltage.nominalVoltage")
            if nv:
                base_voltages[rid] = float(nv)

        elif tag == f"{{{CIM_NS}}}SubGeographicalRegion":
            regions[rid] = _text(elem, "IdentifiedObject.name")

        elif tag == f"{{{CIM_NS}}}VoltageLevel":
            voltage_levels[rid] = {
                "substation_id": _resource(elem, "VoltageLevel.Substation"),
                "base_voltage_id": _resource(elem, "VoltageLevel.BaseVoltage"),
            }

        elif tag == f"{{{CIM_NS}}}Substation":
            name = _text(elem, "IdentifiedObject.name")
            substations[rid] = {
                "name": name,
                "rdf_id": rid,
                "_region_id": _resource(elem, "Substation.Region"),
            }

        elif tag == f"{{{CIM_NS}}}ACLineSegment":
            lines[rid] = {
                "name": _text(elem, "IdentifiedObject.name"),
                "rdf_id": rid,
                "_base_voltage_id": _resource(elem, "ConductingEquipment.BaseVoltage"),
                "length_km": _float(elem, "Conductor.length"),
                "bch": _float(elem, "ACLineSegment.bch"),
                "r_ohm": _float(elem, "ACLineSegment.r"),
                "x_ohm": _float(elem, "ACLineSegment.x"),
                "b0ch": _float(elem, "ACLineSegment.b0ch"),
                "r0_ohm": _float(elem, "ACLineSegment.r0"),
                "x0_ohm": _float(elem, "ACLineSegment.x0"),
                "short_circuit_end_temp_c": _float(elem, "ACLineSegment.shortCircuitEndTemperature"),
            }

        elif tag == f"{{{CIM_NS}}}SynchronousMachine":
            sync_machines[rid] = _text(elem, "IdentifiedObject.name")

        elif tag == f"{{{CIM_NS}}}EnergyConsumer":
            loads[rid] = {
                "name": _text(elem, "IdentifiedObject.name"),
                "rdf_id": rid,
            }

        elif tag == f"{{{CIM_NS}}}PowerElectronicsConnection":
            ctrl_elem = elem.find(f"{{{CIM_NS}}}PowerElectronicsConnection.controlMode")
            ctrl_mode = None
            if ctrl_elem is not None:
                ctrl_uri = ctrl_elem.get(f"{{{RDF_NS}}}resource", "")
                ctrl_mode = ctrl_uri.split(".")[-1] if "." in ctrl_uri else ctrl_uri
            pec_raw[rid] = {
                "name":         _text(elem, "IdentifiedObject.name"),
                "_unit_id":     _resource(elem, "PowerElectronicsConnection.PowerElectronicsUnit"),
                "rated_s":      _float(elem, "PowerElectronicsConnection.ratedS"),
                "rated_u":      _float(elem, "PowerElectronicsConnection.ratedU"),
                "p":            _float(elem, "PowerElectronicsConnection.p"),
                "q":            _float(elem, "PowerElectronicsConnection.q"),
                "max_q":        _float(elem, "PowerElectronicsConnection.maxQ"),
                "min_q":        _float(elem, "PowerElectronicsConnection.minQ"),
                "max_i_fault":  _float(elem, "PowerElectronicsConnection.maxIFault"),
                "control_mode": ctrl_mode,
            }

        elif tag == f"{{{CIM_NS}}}PhotovoltaicUnit":
            pv_unit_raw[rid] = {
                "name":  _text(elem, "IdentifiedObject.name"),
                "max_p": _float(elem, "PowerElectronicsUnit.maxP"),
                "min_p": _float(elem, "PowerElectronicsUnit.minP"),
            }

        elif tag == f"{{{CIM_NS}}}BatteryUnit":
            bat_unit_raw[rid] = {
                "name":    _text(elem, "IdentifiedObject.name"),
                "rated_e": _float(elem, "BatteryUnit.ratedE"),
                "max_p":   _float(elem, "PowerElectronicsUnit.maxP"),
                "min_p":   _float(elem, "PowerElectronicsUnit.minP"),
            }

        elif tag == f"{{{CIM_NS}}}ElectricalCapacity":
            # ENTSO-E extension class — value + explicit unit (e.g. "Ah"). Linked
            # from BatteryStorage.ElectricalCapacity; resolved after the main loop.
            electrical_capacity_raw[rid] = {
                "value": _float(elem, "ElectricalCapacity.value"),
                "unit":  _text(elem, "ElectricalCapacity.unit"),
            }

        elif tag == f"{{{CIM_NS}}}BatteryStorage":
            # ENTSO-E profile extension: a single self-contained class (no separate
            # PowerElectronicsConnection wrapper — BatteryStorage IS the terminal's
            # ConductingEquipment directly). No controlMode field exists on this
            # class; a battery running at a fixed (nominalP, nominalQ) operating
            # point is, by construction, constant-power-factor.
            battery_storage_raw[rid] = {
                "name":              _text(elem, "IdentifiedObject.name"),
                "capacity":          _float(elem, "BatteryStorage.capacity"),
                "p":                 _float(elem, "BatteryStorage.nominalP"),
                "q":                 _float(elem, "BatteryStorage.nominalQ"),
                "rated_s":           _float(elem, "BatteryStorage.ratedS"),
                "rated_power_factor": _float(elem, "BatteryStorage.ratedPowerFactor"),
                "rated_u":           _float(elem, "BatteryStorage.ratedU"),
                "control_mode":      "constantPowerFactor",
                "_capacity_id":      _resource(elem, "BatteryStorage.ElectricalCapacity"),
            }

        elif tag == f"{{{CIM_NS}}}PowerTransformer":
            transformers[rid] = {
                "name": _text(elem, "IdentifiedObject.name"),
                "rdf_id": rid,
            }

        elif tag == f"{{{CIM_NS}}}PowerTransformerEnd":
            ck_elem = elem.find(f"{{{CIM_NS}}}PowerTransformerEnd.connectionKind")
            conn_kind = None
            if ck_elem is not None:
                ck_uri = ck_elem.get(f"{{{RDF_NS}}}resource", "")
                conn_kind = ck_uri.split(".")[-1] if "." in ck_uri else ck_uri

            bv_id = _resource(elem, "TransformerEnd.BaseVoltage")
            transformer_ends[rid] = {
                "transformer_id": _resource(elem, "PowerTransformerEnd.PowerTransformer"),
                "terminal_id": _resource(elem, "TransformerEnd.Terminal"),
                "end_number": int(_text(elem, "TransformerEnd.endNumber") or 0),
                "_base_voltage_id": bv_id,
                "grounded": _text(elem, "TransformerEnd.grounded"),
                "b": _float(elem, "PowerTransformerEnd.b"),
                "connection_kind": conn_kind,
                "rated_s_mva": _float(elem, "PowerTransformerEnd.ratedS"),
                "rated_u_kv": _float(elem, "PowerTransformerEnd.ratedU"),
                "r_ohm": _float(elem, "PowerTransformerEnd.r"),
                "x_ohm": _float(elem, "PowerTransformerEnd.x"),
                "r0_ohm": _float(elem, "PowerTransformerEnd.r0"),
                "x0_ohm": _float(elem, "PowerTransformerEnd.x0"),
                "phase_angle_clock": _float(elem, "PowerTransformerEnd.phaseAngleClock"),
            }

        elif tag == f"{{{CIM_NS}}}Terminal":
            ce_id = _resource(elem, "Terminal.ConductingEquipment")
            seq = _text(elem, "Terminal.sequenceNumber") or _text(elem, "ACDCTerminal.sequenceNumber")
            if ce_id:
                terminals[rid] = {
                    "equipment_id": ce_id,
                    "seq": int(seq) if seq else None,
                }

    if unrecognized_elements:
        detail = "\n".join(f"  - {tag} {name!r}  [{reason}]"
                           for tag, name, reason in unrecognized_elements)
        raise RuntimeError(
            "Unsupported grid element(s) — aborting before any data is written "
            f"to Neo4j:\n{detail}"
        )

    # Resolve nominal voltage for substations via their voltage levels
    for vl in voltage_levels.values():
        sub_id = vl["substation_id"]
        bv_id = vl["base_voltage_id"]
        if sub_id in substations and bv_id in base_voltages:
            substations[sub_id]["nominal_voltage_kv"] = base_voltages[bv_id]

    # VoltageLevel id -> Substation rdf_id. CIM has no direct
    # TopologicalNode -> Substation reference; TopologicalNode.ConnectivityNodeContainer
    # only reaches a VoltageLevel, which then holds VoltageLevel.Substation. This lets
    # parse_topology() resolve each terminal straight to its Substation's own rdf_id
    # instead of matching by display name.
    vl_to_sub = {vl_id: vl["substation_id"] for vl_id, vl in voltage_levels.items()}

    # Resolve region name for substations
    for sub in substations.values():
        region_id = sub.pop("_region_id", None)
        if region_id and region_id in regions:
            sub["region"] = regions[region_id]

    # Resolve nominal voltage for lines
    for line in lines.values():
        bv_id = line.pop("_base_voltage_id", None)
        if bv_id and bv_id in base_voltages:
            line["nominal_voltage_kv"] = base_voltages[bv_id]

    # Aggregate transformer ends onto transformer nodes (HV/LV prefixed)
    for end in transformer_ends.values():
        tr_id = end["transformer_id"]
        if tr_id not in transformers:
            continue
        bv_id = end.pop("_base_voltage_id", None)
        end["nominal_voltage_kv"] = base_voltages.get(bv_id)

    for tr_id, tr in transformers.items():
        ends = [e for e in transformer_ends.values() if e["transformer_id"] == tr_id]
        ends.sort(key=lambda e: (e.get("rated_u_kv") or 0), reverse=True)
        skip = {"transformer_id", "terminal_id", "end_number"}
        for i, end in enumerate(ends):
            prefix = "hv_" if i == 0 else "lv_"
            for k, v in end.items():
                if k not in skip and v is not None:
                    tr[f"{prefix}{k}"] = v

    # Build pv_units and batteries from PowerElectronicsConnection → unit links.
    # Keys are PEC IDs because Terminals point to the PEC (conducting equipment).
    for pec_id, pec in pec_raw.items():
        unit_id = pec.pop("_unit_id", None)
        pec_props = {k: v for k, v in pec.items() if v is not None}
        if unit_id in pv_unit_raw:
            unit = pv_unit_raw[unit_id]
            pv_units[pec_id] = {
                "name":   unit.get("name") or pec_props.get("name"),
                "rdf_id": pec_id,
                "max_p":  unit.get("max_p"),
                "min_p":  unit.get("min_p"),
                **{k: v for k, v in pec_props.items() if k != "name"},
            }
        elif unit_id in bat_unit_raw:
            unit = bat_unit_raw[unit_id]
            batteries[pec_id] = {
                "name":    unit.get("name") or pec_props.get("name"),
                "rdf_id":  pec_id,
                "rated_e": unit.get("rated_e"),
                "max_p":   unit.get("max_p"),
                "min_p":   unit.get("min_p"),
                **{k: v for k, v in pec_props.items() if k != "name"},
            }

    # Build batteries from the ENTSO-E BatteryStorage extension class — a
    # separate, self-contained pattern (no PowerElectronicsConnection wrapper).
    # Mapped onto the same property shape as the BatteryUnit+PEC pattern above
    # so downstream code (battery_simulator.py, scenario_adaptive.py) needs no
    # changes regardless of which CIM pattern a given source file uses.
    for bs_id, bs in battery_storage_raw.items():
        cap_id = bs.pop("_capacity_id", None)
        cap = electrical_capacity_raw.get(cap_id) if cap_id else None
        nominal_p = bs.get("p")
        batteries[bs_id] = {
            "name":     bs.get("name"),
            "rdf_id":   bs_id,
            "rated_e":  bs.get("capacity"),
            # BatteryStorage has no separate maxP/minP fields (unlike BatteryUnit) —
            # inferred as symmetric charge/discharge capability around nominalP.
            "max_p":    nominal_p,
            "min_p":    -nominal_p if nominal_p is not None else None,
            "rated_s":  bs.get("rated_s"),
            "rated_u":  bs.get("rated_u"),
            "p":        bs.get("p"),
            "q":        bs.get("q"),
            "control_mode": bs.get("control_mode"),
            # Explicit unit, when the ElectricalCapacity link resolves — solves
            # the kW-vs-MW ambiguity the BatteryUnit pattern doesn't disclose.
            "capacity_ah":   cap.get("value") if cap else None,
            "capacity_unit": cap.get("unit") if cap else None,
        }
        batteries[bs_id] = {k: v for k, v in batteries[bs_id].items() if v is not None}

    # Remove None-valued properties
    substations = {k: {pk: pv for pk, pv in v.items() if pv is not None} for k, v in substations.items()}
    lines = {k: {pk: pv for pk, pv in v.items() if pv is not None} for k, v in lines.items()}
    transformers = {k: {pk: pv for pk, pv in v.items() if pv is not None} for k, v in transformers.items()}

    return (substations, lines, terminals, transformers, transformer_ends, loads,
            batteries, pv_units, sync_machines, vl_to_sub)

def parse_state_variables():
    """
    Parse StateVariables XML and return:
      sv_flows   : dict  terminal_id -> {p_mw, q_mvar}
      slack_nodes: set   of topological node IDs whose SvVoltage.angle == 0
                         (these are the reference/slack buses)
      sv_voltages: dict  topological_node_id -> v_kv  (from SvVoltage.v, which is
                         in kV per CGMES convention)
    """
    root = et_parse(SV_FILE).getroot()
    sv_flows = {}
    slack_nodes = set()
    sv_voltages = {}   # node_id -> v_kv

    sv_angles = {}   # node_id -> angle_deg

    for elem in root:
        if elem.tag == f"{{{CIM_NS}}}SvVoltage":
            angle_str = elem.findtext(f"{{{CIM_NS}}}SvVoltage.angle")
            v_str     = elem.findtext(f"{{{CIM_NS}}}SvVoltage.v")
            node_ref  = elem.find(f"{{{CIM_NS}}}SvVoltage.TopologicalNode")
            if node_ref is not None:
                node_id = node_ref.get(f"{{{RDF_NS}}}resource", "").lstrip("#")
                if v_str is not None:
                    try:
                        sv_voltages[node_id] = float(v_str)
                    except ValueError:
                        pass
                if angle_str is not None:
                    try:
                        angle = float(angle_str)
                    except ValueError:
                        angle = None
                    if angle is not None:
                        sv_angles[node_id] = angle
                        if angle == 0.0:
                            slack_nodes.add(node_id)

        elif elem.tag == f"{{{CIM_NS}}}SvPowerFlow":
            tid_elem = elem.find(f"{{{CIM_NS}}}SvPowerFlow.Terminal")
            if tid_elem is None:
                continue
            tid = tid_elem.get(f"{{{RDF_NS}}}resource", "").lstrip("#")
            p = elem.findtext(f"{{{CIM_NS}}}SvPowerFlow.p")
            q = elem.findtext(f"{{{CIM_NS}}}SvPowerFlow.q")
            sv_flows[tid] = {
                "p_mw": float(p) if p is not None else None,
                "q_mvar": float(q) if q is not None else None,
            }

    return sv_flows, slack_nodes, sv_voltages, sv_angles

def parse_topology(vl_to_sub):
    """
    Parse the Topology XML and return two mappings:
      terminal_to_node : terminal_id -> topological_node_id (raw TopologicalNode
                         identifier, e.g. 'BUS_1   69.0'). Built from
                         Terminal.TopologicalNode references.
      terminal_to_sub  : terminal_id -> substation rdf_id, resolved via
                         TopologicalNode.ConnectivityNodeContainer -> VoltageLevel
                         -> VoltageLevel.Substation (*vl_to_sub*, from
                         parse_equipment()). No name matching involved.
    """
    root = et_parse(TP_FILE).getroot()

    # TopologicalNode id -> its containing VoltageLevel id.
    node_to_vl = {}
    for elem in root:
        if elem.tag != f"{{{CIM_NS}}}TopologicalNode":
            continue
        nid = _rdf_id(elem)
        vl_id = _resource(elem, "TopologicalNode.ConnectivityNodeContainer")
        if vl_id:
            node_to_vl[nid] = vl_id

    terminal_to_node = {}
    terminal_to_sub = {}
    for elem in root:
        if elem.tag != f"{{{CIM_NS}}}Terminal":
            continue
        tid = _rdf_id(elem)
        node_ref = elem.find(f"{{{CIM_NS}}}Terminal.TopologicalNode")
        if node_ref is not None:
            node_id = node_ref.get(f"{{{RDF_NS}}}resource", "").lstrip("#")
            terminal_to_node[tid] = node_id
            vl_id = node_to_vl.get(node_id)
            sub_id = vl_to_sub.get(vl_id) if vl_id else None
            if sub_id:
                terminal_to_sub[tid] = sub_id

    return terminal_to_node, terminal_to_sub

def build_battery_connections(batteries, terminals, terminal_to_sub, sv_flows):
    """
    For each BatteryStorage, find the connected substation via its terminal
    and attach SvPowerFlow p/q values (operating point from load-flow results).

    Returns a list of (battery_id, battery_props_dict, substation_rdf_id, terminal_id).
    """
    battery_terminals = {}
    for tid, term in terminals.items():
        if term["equipment_id"] in batteries:
            battery_terminals[term["equipment_id"]] = tid

    connections = []
    for bat_id, bat in batteries.items():
        tid = battery_terminals.get(bat_id)
        if tid is None:
            continue
        sub_id = terminal_to_sub.get(tid)
        if sub_id is None:
            continue
        flow = sv_flows.get(tid, {})
        bat_props = {**bat, "terminal_id": tid}
        if flow.get("p_mw") is not None:
            bat_props["sv_p_mw"] = flow["p_mw"]
        if flow.get("q_mvar") is not None:
            bat_props["sv_q_mvar"] = flow["q_mvar"]
        connections.append((bat_id, bat_props, sub_id, tid))

    return connections


def build_pv_connections(pv_units, terminals, terminal_to_sub, sv_flows):
    """
    For each PhotovoltaicUnit, find the connected substation via its terminal.

    Returns a list of (pv_id, pv_props_dict, substation_rdf_id, terminal_id).
    """
    pv_terminals = {}
    for tid, term in terminals.items():
        if term["equipment_id"] in pv_units:
            pv_terminals[term["equipment_id"]] = tid

    connections = []
    for pv_id, pv in pv_units.items():
        tid = pv_terminals.get(pv_id)
        if tid is None:
            continue
        sub_id = terminal_to_sub.get(tid)
        if sub_id is None:
            continue
        flow = sv_flows.get(tid, {})
        pv_props = {**pv, "terminal_id": tid}
        if flow.get("p_mw") is not None:
            pv_props["sv_p_mw"] = flow["p_mw"]
        if flow.get("q_mvar") is not None:
            pv_props["sv_q_mvar"] = flow["q_mvar"]
        connections.append((pv_id, pv_props, sub_id, tid))

    return connections


def build_load_connections(loads, terminals, terminal_to_sub, sv_flows):
    """
    For each EnergyConsumer (load), find the connected substation and
    attach SvPowerFlow p/q values.

    Returns a list of (load_id, load_props_dict, substation_rdf_id, terminal_id).
    """
    load_terminals = {}
    for tid, term in terminals.items():
        if term["equipment_id"] in loads:
            load_terminals[term["equipment_id"]] = tid

    connections = []
    for load_id, load in loads.items():
        tid = load_terminals.get(load_id)
        if tid is None:
            continue
        sub_id = terminal_to_sub.get(tid)
        if sub_id is None:
            continue
        flow = sv_flows.get(tid, {})
        load_props = {
            **load,
            "terminal_id": tid,
        }
        if flow.get("p_mw") is not None:
            load_props["p_mw"] = flow["p_mw"]
        if flow.get("q_mvar") is not None:
            load_props["q_mvar"] = flow["q_mvar"]
        connections.append((load_id, load_props, sub_id, tid))

    return connections


def build_connections(lines, terminals, terminal_to_sub):
    """
    For each ACLineSegment, find the substation at each end using terminal
    sequence numbers (1 = from-side, 2 = to-side).

    Returns a list of (line_id, from_sub_rdf_id, from_tid, to_sub_rdf_id, to_tid).
    """
    endpoints = {}
    for term_id, term in terminals.items():
        eq_id = term["equipment_id"]
        if eq_id not in lines:
            continue
        sub_id = terminal_to_sub.get(term_id)
        if sub_id is None:
            continue
        endpoints.setdefault(eq_id, {})[term["seq"]] = (sub_id, term_id)

    connections = []
    for line_id, ep in endpoints.items():
        from_sub, from_tid = ep.get(1, (None, None))
        to_sub, to_tid = ep.get(2, (None, None))
        connections.append((line_id, from_sub, from_tid, to_sub, to_tid))

    return connections


def build_transformer_connections(transformers, transformer_ends, terminals, terminal_to_sub):
    """
    For each PowerTransformer, find the substation at each end.

    Returns a list of (tr_id, hv_sub_rdf_id, hv_tid, lv_sub_rdf_id, lv_tid).
    """
    endpoints = {}
    for end in transformer_ends.values():
        tr_id = end["transformer_id"]
        if tr_id not in transformers:
            continue
        tid = end["terminal_id"]
        sub_id = terminal_to_sub.get(tid)
        if sub_id is None:
            continue
        endpoints.setdefault(tr_id, {})[end["end_number"]] = (sub_id, tid)

    connections = []
    for tr_id, ep in endpoints.items():
        hv_sub, hv_tid = ep.get(1, (None, None))
        lv_sub, lv_tid = ep.get(2, (None, None))
        connections.append((tr_id, hv_sub, hv_tid, lv_sub, lv_tid))

    return connections


def push_to_neo4j(substations, lines, connections, transformers, tr_connections,
                  loads, load_connections, batteries, battery_connections,
                  pv_units, pv_connections, scheme_name: str = ""):
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USERNAME, NEO4J_PASSWORD))

    with driver.session(database=NEO4J_DATABASE) as session:
        # Clear existing data
        session.execute_write(lambda tx: tx.run("MATCH (n) DETACH DELETE n"))
        print("  Cleared existing graph.")

        # Write scheme metadata so visualize.py can label the legend correctly.
        import datetime
        session.execute_write(
            lambda tx: tx.run(
                "CREATE (m:GridMetadata {scheme_name: $name, source: $src, loaded_at: $ts})",
                name=scheme_name,
                src=_cim_arg,
                ts=datetime.datetime.now().isoformat(timespec='seconds'),
            )
        )
        print(f"  Stored GridMetadata: scheme_name={scheme_name!r}")

        # Batch-create Substation nodes (includes is_slack and nominal_voltage_kv)
        session.execute_write(
            lambda tx: tx.run(
                "UNWIND $rows AS props CREATE (n:Substation) SET n = props",
                rows=list(substations.values()),
            )
        )
        print(f"  Created {len(substations)} Substation nodes.")
        slack_names = [s["name"] for s in substations.values() if s.get("is_slack")]
        print(f"  Slack buses: {slack_names}")

        # Batch-create Transformer nodes
        session.execute_write(
            lambda tx: tx.run(
                "UNWIND $rows AS props CREATE (n:Transformer) SET n = props",
                rows=list(transformers.values()),
            )
        )
        print(f"  Created {len(transformers)} Transformer nodes.")

        # Batch-create LINE relationships
        line_rows = [
            {
                "from_node": from_node,
                "to_node": to_node,
                "props": {
                    **lines.get(line_id, {}),
                    "from_terminal_id": from_tid,
                    "to_terminal_id": to_tid,
                },
            }
            for line_id, from_node, from_tid, to_node, to_tid in connections
            if from_node and to_node
        ]
        session.execute_write(
            lambda tx: tx.run(
                """
                UNWIND $rows AS row
                MATCH (a:Substation {rdf_id: row.from_node})
                MATCH (b:Substation {rdf_id: row.to_node})
                CREATE (a)-[r:LINE]->(b)
                SET r = row.props
                """,
                rows=line_rows,
            )
        )
        print(f"  Created {len(line_rows)} LINE relationships.")

        # Batch-create HV and LV transformer CONNECT_TO relationships
        hv_rows = [
            {"node": hv_node, "tid": tr_id, "term_id": hv_tid}
            for tr_id, hv_node, hv_tid, lv_node, lv_tid in tr_connections
            if hv_node
        ]
        lv_rows = [
            {"node": lv_node, "tid": tr_id, "term_id": lv_tid}
            for tr_id, hv_node, hv_tid, lv_node, lv_tid in tr_connections
            if lv_node
        ]
        session.execute_write(
            lambda tx: tx.run(
                """
                UNWIND $rows AS row
                MATCH (s:Substation {rdf_id: row.node})
                MATCH (t:Transformer {rdf_id: row.tid})
                CREATE (s)-[:CONNECT_TO {terminal_id: row.term_id, side: 'HV'}]->(t)
                """,
                rows=hv_rows,
            )
        )
        session.execute_write(
            lambda tx: tx.run(
                """
                UNWIND $rows AS row
                MATCH (t:Transformer {rdf_id: row.tid})
                MATCH (s:Substation {rdf_id: row.node})
                CREATE (t)-[:CONNECT_TO {terminal_id: row.term_id, side: 'LV'}]->(s)
                """,
                rows=lv_rows,
            )
        )
        print(f"  Created {len(hv_rows) + len(lv_rows)} transformer CONNECT_TO relationships.")

        # Batch-create Load nodes
        session.execute_write(
            lambda tx: tx.run(
                "UNWIND $rows AS props CREATE (n:Load) SET n = props",
                rows=[lp for _, lp, _, _ in load_connections],
            )
        )
        print(f"  Created {len(load_connections)} Load nodes.")

        # Batch-create Load CONNECT_TO relationships
        load_rows = [
            {"node": node, "rdf_id": load_props["rdf_id"], "tid": tid}
            for _, load_props, node, tid in load_connections
        ]
        session.execute_write(
            lambda tx: tx.run(
                """
                UNWIND $rows AS row
                MATCH (s:Substation {rdf_id: row.node})
                MATCH (l:Load {rdf_id: row.rdf_id})
                CREATE (s)-[:CONNECT_TO {terminal_id: row.tid}]->(l)
                """,
                rows=load_rows,
            )
        )
        print(f"  Created {len(load_rows)} Load CONNECT_TO relationships.")

        # Batch-create Battery nodes
        if battery_connections:
            session.execute_write(
                lambda tx: tx.run(
                    "UNWIND $rows AS props CREATE (n:Battery) SET n = props",
                    rows=[bp for _, bp, _, _ in battery_connections],
                )
            )
            print(f"  Created {len(battery_connections)} Battery nodes.")

            # Batch-create Battery CONNECT_TO relationships (Substation → Battery)
            bat_rows = [
                {"node": node, "rdf_id": bat_props["rdf_id"], "tid": tid}
                for _, bat_props, node, tid in battery_connections
            ]
            session.execute_write(
                lambda tx: tx.run(
                    """
                    UNWIND $rows AS row
                    MATCH (s:Substation {rdf_id: row.node})
                    MATCH (b:Battery {rdf_id: row.rdf_id})
                    CREATE (s)-[:CONNECT_TO {terminal_id: row.tid}]->(b)
                    """,
                    rows=bat_rows,
                )
            )
            print(f"  Created {len(bat_rows)} Battery CONNECT_TO relationships.")
        else:
            print("  No Battery nodes found in CIM — skipping.")

        # Batch-create PV nodes
        if pv_connections:
            session.execute_write(
                lambda tx: tx.run(
                    "UNWIND $rows AS props CREATE (n:PV) SET n = props",
                    rows=[pp for _, pp, _, _ in pv_connections],
                )
            )
            print(f"  Created {len(pv_connections)} PV nodes.")

            # Batch-create PV CONNECT_TO relationships (Substation → PV)
            pv_rows = [
                {"node": node, "rdf_id": pv_props["rdf_id"], "tid": tid}
                for _, pv_props, node, tid in pv_connections
            ]
            session.execute_write(
                lambda tx: tx.run(
                    """
                    UNWIND $rows AS row
                    MATCH (s:Substation {rdf_id: row.node})
                    MATCH (p:PV {rdf_id: row.rdf_id})
                    CREATE (s)-[:CONNECT_TO {terminal_id: row.tid}]->(p)
                    """,
                    rows=pv_rows,
                )
            )
            print(f"  Created {len(pv_rows)} PV CONNECT_TO relationships.")
        else:
            print("  No PV nodes found in CIM — skipping.")

    driver.close()


def ingest(cim_source):
    """Parse a CIM source (file or folder) and push the resulting grid into Neo4j."""
    global EQ_FILE, TP_FILE, SV_FILE, _SCHEME_NAME, _cim_arg

    _cim_arg = cim_source if os.path.isabs(cim_source) else os.path.join(PROJECT_DIR, cim_source)

    _scheme_raw = os.path.splitext(os.path.basename(os.path.normpath(_cim_arg)))[0]
    _SCHEME_NAME = _scheme_raw.replace('_', ' ')
    if os.path.isfile(_cim_arg) and _cim_arg.endswith(".xml"):
        # Single combined XML file: all profiles are in one file.
        # Each parse_*() function filters by element tag, so pointing all three
        # file handles at the same path works transparently.
        EQ_FILE = TP_FILE = SV_FILE = _cim_arg
        print(f"Single-file mode: {_cim_arg}")
    else:
        EQ_FILE, TP_FILE, SV_FILE = _find_cgmes_files(_cim_arg)
        print(f"CIM files: EQ={EQ_FILE}, TP={TP_FILE}, SV={SV_FILE}")

    print("Parsing Equipment XML …")
    (substations, lines, terminals, transformers, transformer_ends, loads,
     batteries, pv_units, sync_machines, vl_to_sub) = parse_equipment()
    print(f"  {len(substations)} substations, {len(lines)} ACLineSegments, "
          f"{len(transformers)} transformers, {len(terminals)} terminals, "
          f"{len(loads)} loads, {len(batteries)} Battery (PEC), "
          f"{len(pv_units)} PV (PEC), "
          f"{len(sync_machines)} SynchronousMachines")

    print("Parsing Topology XML …")
    terminal_to_node, terminal_to_sub = parse_topology(vl_to_sub)
    print(f"  {len(terminal_to_node)} terminal→node mappings, "
          f"{len(terminal_to_sub)} terminal→substation(rdf_id) mappings")

    print("Parsing StateVariables XML …")
    sv_flows, slack_nodes, sv_voltages, sv_angles = parse_state_variables()
    print(f"  {len(sv_flows)} SvPowerFlow records")
    print(f"  {len(sv_voltages)} SvVoltage records")
    print(f"  {len(sv_angles)} SvVoltage angle records")
    print(f"  Slack nodes detected (angle=0): {slack_nodes}")

    # Build node_id → substation name mapping via terminal_to_node
    # terminal_to_node maps terminal_id → node_id (the rdf:about of the TopologicalNode)
    # We need to find the node_id for each substation by looking at its terminals.
    # Substations don't have terminals directly; voltage levels have conducting equipment.
    # The simplest approach: parse the topology file's TopologicalNode IdentifiedObject.name
    # to map node_id → human-readable name like "BUS_1   69.0".
    tp_root = et_parse(TP_FILE).getroot()
    node_id_to_name = {}
    for elem in tp_root:
        if elem.tag == f"{{{CIM_NS}}}TopologicalNode":
            nid = _rdf_id(elem)
            nname = elem.findtext(f"{{{CIM_NS}}}IdentifiedObject.name")
            if nid and nname:
                node_id_to_name[nid] = nname

    # Tag substations with is_slack, sv_voltage_kv, and sv_angle_deg
    for sub in substations.values():
        sub["is_slack"] = sub.get("name") in slack_nodes
        # Look up sv_voltage_kv / sv_angle_deg: find the node whose name matches this substation
        matching_node_id = next(
            (nid for nid, nname in node_id_to_name.items() if nname == sub["name"]),
            None,
        )
        if matching_node_id is None:
            # Fallback: try matching by node_id == name directly (NEPLAN convention)
            matching_node_id = sub["name"]
        v_kv = sv_voltages.get(matching_node_id)
        if v_kv is not None:
            sub["sv_voltage_kv"] = v_kv
        ang = sv_angles.get(matching_node_id)
        if ang is not None:
            sub["sv_angle_deg"] = ang

    # Tag PV buses: substations where a SynchronousMachine (voltage-regulating
    # generator) is connected.  These hold voltage magnitude like a slack bus.
    # Build: sm_id -> bus_name via terminals + topology
    sm_bus_names = set()
    sm_bus_p_gen = {}   # bus_name -> total p_gen_mw (positive = injection into network)
    sm_bus_q_gen = {}   # bus_name -> total q_gen_mvar (positive = injection into network)
    for sm_id in sync_machines:
        for tid, term in terminals.items():
            if term["equipment_id"] == sm_id:
                node_id = terminal_to_node.get(tid)
                bus_name = node_id_to_name.get(node_id)
                if bus_name:
                    sm_bus_names.add(bus_name)
                    # CGMES SvPowerFlow sign: positive p = power INTO element (load conv.)
                    # Generator injects -> negative SvPowerFlow.p -> negate for p_gen
                    flow = sv_flows.get(tid, {})
                    p_raw = flow.get('p_mw')
                    q_raw = flow.get('q_mvar')
                    if p_raw is not None:
                        sm_bus_p_gen[bus_name] = sm_bus_p_gen.get(bus_name, 0.0) + (-p_raw)
                    if q_raw is not None:
                        sm_bus_q_gen[bus_name] = sm_bus_q_gen.get(bus_name, 0.0) + (-q_raw)
                break

    for sub in substations.values():
        sub["is_sync_machine"] = sub.get("name") in sm_bus_names and not sub.get("is_slack", False)
        if sub.get("is_sync_machine"):
            sub["p_gen_mw"]   = sm_bus_p_gen.get(sub["name"], 0.0)
            sub["q_gen_mvar"] = sm_bus_q_gen.get(sub["name"], 0.0)
    print(f"  SynchronousMachine buses detected: {sorted(sm_bus_names - slack_nodes)}")

    # For log readability: substation rdf_id -> display name (connections now
    # carry rdf_id, not the bus name, as their substation reference).
    sub_name_by_rdfid = {s["rdf_id"]: s["name"] for s in substations.values()}

    print("Building line connections …")
    connections = build_connections(lines, terminals, terminal_to_sub)
    print(f"  {len(connections)} ACLineSegment connections")

    print("Building transformer connections …")
    tr_connections = build_transformer_connections(transformers, transformer_ends, terminals, terminal_to_sub)
    print(f"  {len(tr_connections)} transformer connections")

    print("Building load connections …")
    load_connections = build_load_connections(loads, terminals, terminal_to_sub, sv_flows)
    print(f"  {len(load_connections)} load connections")

    print("Building battery connections …")
    battery_connections = build_battery_connections(batteries, terminals, terminal_to_sub, sv_flows)
    print(f"  {len(battery_connections)} battery connections")
    for _, bp, sub_id, _ in battery_connections:
        print(f"  Battery '{bp['name']}' at {sub_name_by_rdfid.get(sub_id, sub_id)}: "
              f"ratedE={bp.get('rated_e')}, "
              f"maxP={bp.get('max_p')}, "
              f"ratedS={bp.get('rated_s')}, "
              f"ratedU={bp.get('rated_u')}, "
              f"controlMode={bp.get('control_mode')}")

    print("Building PV connections …")
    pv_connections = build_pv_connections(pv_units, terminals, terminal_to_sub, sv_flows)
    print(f"  {len(pv_connections)} PV connections")
    for _, pp, sub_id, _ in pv_connections:
        print(f"  PV '{pp['name']}' at {sub_name_by_rdfid.get(sub_id, sub_id)}: "
              f"maxP={pp.get('max_p')}, "
              f"ratedS={pp.get('rated_s')}, "
              f"ratedU={pp.get('rated_u')}, "
              f"controlMode={pp.get('control_mode')}")

    print("Pushing to Neo4j Aura …")
    push_to_neo4j(substations, lines, connections, transformers, tr_connections,
                  loads, load_connections, batteries, battery_connections,
                  pv_units, pv_connections, scheme_name=_SCHEME_NAME)


# ============================================================================
# Step 2/3 -- shared GraphDB helpers (from src/scenario_adaptive.py)
# ============================================================================

def _neo4j_session():
    load_dotenv(os.path.join(PROJECT_DIR, '.env'))
    driver = GraphDatabase.driver(
        os.getenv('NEO4J_URI'),
        auth=(os.getenv('NEO4J_USERNAME'), os.getenv('NEO4J_PASSWORD')),
    )
    return driver, driver.session(database=os.getenv('NEO4J_DATABASE'))


# ── Naming helpers (mirrors connect.py's variable-naming convention) ──────────

def _var(name: str) -> str:
    return name.lower().replace('-', '_').replace(' ', '_')


def _sub_var(node_name: str) -> str:
    return f'sub_{_var(node_name)}'


def _line_var(from_name: str, to_name: str) -> str:
    return f'line_{_var(from_name)}_{_var(to_name)}'


def _load_var(load_name: str) -> str:
    return _var(load_name)


def _tr_var(tr_name: str) -> str:
    return _var(tr_name)


def fetch_edges():
    """Return (line_edges, load_edges, transformer_edges) from Neo4j."""
    driver, session = _neo4j_session()
    try:
        line_edges = session.run(
            'MATCH (a:Substation)-[l:LINE]->(b:Substation) '
            'RETURN a.name AS from_sub, b.name AS to_sub '
            'ORDER BY a.name, b.name'
        ).data()

        load_edges = session.run(
            'MATCH (s:Substation)-[:CONNECT_TO]->(l:Load) '
            'RETURN s.name AS sub_name, l.name AS load_name '
            'ORDER BY s.name, l.name'
        ).data()

        transformer_edges = session.run(
            'MATCH (hv:Substation)-[:CONNECT_TO {side:"HV"}]->(t:Transformer)'
            '-[:CONNECT_TO {side:"LV"}]->(lv:Substation) '
            'RETURN hv.name AS hv_sub, t.name AS tr_name, lv.name AS lv_sub '
            'ORDER BY t.name'
        ).data()
    finally:
        session.close()
        driver.close()

    return line_edges, load_edges, transformer_edges


def fetch_all_network_params():
    """
    Query Neo4j generically for substations, transformers, lines, loads.
    Returns (sub_params, transformers, line_params, load_params).
    """
    driver, session = _neo4j_session()
    try:
        sub_records = session.run(
            'MATCH (s:Substation) '
            'RETURN s.name AS name, s.nominal_voltage_kv AS v_nom, '
            '       s.is_slack AS is_slack, s.is_sync_machine AS is_sync_machine, '
            '       s.sv_voltage_kv AS sv_v, s.sv_angle_deg AS sv_ang, '
            '       s.p_gen_mw AS p_gen_mw, s.q_gen_mvar AS q_gen_mvar '
            'ORDER BY s.name'
        ).data()
        sub_params = {
            r['name']: {
                'v_nom_kv':        r['v_nom']      if r['v_nom']      is not None else 20.0,
                'is_slack':        bool(r['is_slack'])        if r['is_slack']        is not None else False,
                'is_sync_machine': bool(r['is_sync_machine']) if r['is_sync_machine'] is not None else False,
                'sv_voltage_kv':   r['sv_v']        if r['sv_v']        is not None else None,
                'sv_angle_deg':    r['sv_ang']      if r['sv_ang']      is not None else 0.0,
                'p_gen_mw':        r['p_gen_mw']    if r['p_gen_mw']    is not None else 0.0,
                'q_gen_mvar':      r['q_gen_mvar']  if r['q_gen_mvar']  is not None else 0.0,
            }
            for r in sub_records
        }
        if not sub_params:
            raise RuntimeError('No Substation nodes found in graph database')

        # Transformer, LINE, and Load are all optional — same as Battery/PV.
        # A single-voltage-level grid with no transformers, or a network under
        # study with no load yet, is a physically valid topology. The real
        # requirement (a slack bus reachable from every simulated bus) is
        # checked below, once connectivity is known.
        tr_records   = session.run('MATCH (t:Transformer) RETURN t ORDER BY t.name').data()
        transformers = [dict(r['t']) for r in tr_records]

        line_records = session.run(
            'MATCH (a:Substation)-[l:LINE]->(b:Substation) '
            'RETURN a.name AS from_sub, b.name AS to_sub, '
            '       l.r_ohm AS r_ohm, l.x_ohm AS x_ohm, l.bch AS bch '
            'ORDER BY a.name, b.name'
        ).data()
        line_params = {
            (r['from_sub'], r['to_sub']): {'r_ohm': r['r_ohm'], 'x_ohm': r['x_ohm'], 'bch': r['bch']}
            for r in line_records
        }

        load_records = session.run(
            'MATCH (l:Load) RETURN l.name AS name, l.p_mw AS p_mw, l.q_mvar AS q_mvar '
            'ORDER BY l.name'
        ).data()
        load_params = {r['name']: {'p_mw': r['p_mw'], 'q_mvar': r['q_mvar']} for r in load_records}

    finally:
        session.close()
        driver.close()

    return sub_params, transformers, line_params, load_params


def _fetch_device_buses(label: str):
    """
    Return list of Substation names connected to nodes of *label* via CONNECT_TO.
    Returns an empty list if no such nodes exist (never raises).
    """
    driver, session = _neo4j_session()
    try:
        records = session.run(
            f'MATCH (s:Substation)-[:CONNECT_TO]->(d:{label}) '
            'RETURN s.name AS name ORDER BY s.name'
        ).data()
        return [r['name'] for r in records]
    finally:
        session.close()
        driver.close()


def _fetch_battery_power_factor() -> float:
    """
    Read the CIM-specified constant power factor for the first Battery node
    (PowerElectronicsConnection.p / .q, controlMode=constantPowerFactor).
    Falls back to 1.0 (unity) if the data isn't present or the control mode
    isn't constantPowerFactor.
    """
    driver, session = _neo4j_session()
    try:
        rec = session.run(
            'MATCH (b:Battery) RETURN b.p AS p, b.q AS q, b.control_mode AS mode '
            'ORDER BY b.name LIMIT 1'
        ).single()
        if not rec or rec['mode'] != 'constantPowerFactor' or rec['p'] is None or rec['q'] is None:
            return 1.0
        p, q = rec['p'], rec['q']
        s = (p ** 2 + q ** 2) ** 0.5
        return p / s if s > 1e-9 else 1.0
    finally:
        session.close()
        driver.close()


def _fetch_scheme_name() -> str:
    """Read the scheme name recorded by add_data_slack_detection.py (GridMetadata node)."""
    driver, session = _neo4j_session()
    try:
        rec = session.run(
            'MATCH (m:GridMetadata) RETURN m.scheme_name AS name ORDER BY m.loaded_at DESC LIMIT 1'
        ).single()
        return rec['name'] if rec and rec['name'] else ''
    finally:
        session.close()
        driver.close()


# ============================================================================
# Step 2 -- topology preview (from src/connect.py)
# ============================================================================

def gen_transformer_connections(transformer_edges: list) -> list[str]:
    """V_source → Transformer voltage connections."""
    lines = []
    for tr in transformer_edges:
        tr_v = _tr_var(tr['tr_name'])
        lines.append(f'# {tr["tr_name"]}: HV bus {tr["hv_sub"]} / LV bus {tr["lv_sub"]}')
        lines.append(f"world.connect(v_source, {tr_v}, ('V_source_mag',   'V1_mag'))")
        lines.append(f"world.connect(v_source, {tr_v}, ('V_source_angle', 'V1_angle'))")
        lines.append('')
    return lines


def gen_line_connections(line_edges: list, mv_voltage_var: str = 'V_MV_KV') -> list[str]:
    """
    For each LINE edge:
      - from-substation → line : from-bus voltages (time_shifted)
      - to-substation   → line : to-bus  voltages (time_shifted)
      - line            → to-substation : injected currents
    """
    lines = []
    for edge in line_edges:
        fs  = edge['from_sub']
        ts  = edge['to_sub']
        fv  = _sub_var(fs)
        tv  = _sub_var(ts)
        lv  = _line_var(fs, ts)

        lines.append(f'# Line {fs} → {ts}')
        lines.append(
            f"world.connect({fv}, {lv}, ('V_mag_kv', 'V_from_mag_kv'), "
            f"time_shifted=True, initial_data={{'V_mag_kv': {mv_voltage_var}}})"
        )
        lines.append(
            f"world.connect({fv}, {lv}, ('V_ang_deg', 'V_from_ang_deg'), "
            f"time_shifted=True, initial_data={{'V_ang_deg': 0.0}})"
        )
        lines.append(
            f"world.connect({tv}, {lv}, ('V_mag_kv', 'V_to_mag_kv'),   "
            f"time_shifted=True, initial_data={{'V_mag_kv': {mv_voltage_var}}})"
        )
        lines.append(
            f"world.connect({tv}, {lv}, ('V_ang_deg', 'V_to_ang_deg'), "
            f"time_shifted=True, initial_data={{'V_ang_deg': 0.0}})"
        )
        lines.append(f"world.connect({lv}, {tv}, ('I_to_re', 'I_in_re'))")
        lines.append(f"world.connect({lv}, {tv}, ('I_to_im', 'I_in_im'))")
        lines.append('')
    return lines


def gen_load_connections(load_edges: list) -> list[str]:
    """Load → Substation P/Q connections."""
    lines = []
    for edge in load_edges:
        lv = _load_var(edge['load_name'])
        sv = _sub_var(edge['sub_name'])
        lines.append(f'# {edge["load_name"]} → {edge["sub_name"]}')
        lines.append(f"world.connect({lv}, {sv}, ('P_load_mw',   'P_load_mw'))")
        lines.append(f"world.connect({lv}, {sv}, ('Q_load_mvar', 'Q_load_mvar'))")
        lines.append('')
    return lines


def gen_collector_connections(
    transformer_edges: list,
    line_edges: list,
) -> list[str]:
    """
    Collector monitoring connections:
      - Slack bus (LV side of each transformer) → V_mag_kv
      - Each substation on a LINE → V_mag_kv, V_ang_deg
      - Each LINE → P_loss_mw, Q_loss_mvar
      - v_source → V_source_mag (HV monitoring)
      - Each transformer → V2 (LV voltage monitoring)
    """
    lines = []

    # Slack buses = LV side of each transformer
    lines.append('# Slack bus voltage magnitude')
    for tr in transformer_edges:
        sv = _sub_var(tr['lv_sub'])
        key = f'V_slack_{sv}_mag_kv'
        lines.append(f"world.connect({sv}, collector, ('V_mag_kv', '{key}'))")
    lines.append('')

    # Load buses = all substations appearing in LINE edges (unique, ordered)
    seen: set[str] = set()
    bus_order: list[str] = []
    for edge in line_edges:
        for name in (edge['from_sub'], edge['to_sub']):
            if name not in seen:
                seen.add(name)
                bus_order.append(name)

    lines.append('# Load bus voltages (magnitude + angle)')
    for name in bus_order:
        sv  = _sub_var(name)
        mag_key = f'V_{sv}_mag_kv'
        ang_key = f'V_{sv}_ang_deg'
        lines.append(f"world.connect({sv}, collector, ('V_mag_kv', '{mag_key}'))")
        lines.append(f"world.connect({sv}, collector, ('V_ang_deg', '{ang_key}'))")
    lines.append('')

    # Line losses
    lines.append('# Line active / reactive power losses')
    for edge in line_edges:
        lv       = _line_var(edge['from_sub'], edge['to_sub'])
        p_key    = f'P_loss_{lv}_mw'
        q_key    = f'Q_loss_{lv}_mvar'
        lines.append(f"world.connect({lv}, collector, ('P_loss_mw',   '{p_key}'))")
        lines.append(f"world.connect({lv}, collector, ('Q_loss_mvar', '{q_key}'))")
    lines.append('')

    # HV source monitoring
    lines.append('# HV source monitoring')
    lines.append("world.connect(v_source, collector, ('V_source_mag', 'V_HV_mag'))")
    lines.append('')

    # Transformer LV voltage monitoring
    lines.append('# Transformer LV voltage monitoring')
    for tr in transformer_edges:
        tr_v   = _tr_var(tr['tr_name'])
        tr_key = f'V_{tr_v}_LV'
        lines.append(f"world.connect({tr_v}, collector, ('V2', '{tr_key}'))")
    lines.append('')

    return lines

def preview_connections():
    """Print the mosaik world.connect() statements the current graph would produce."""
    print('Fetching edges from GraphDB...')
    line_edges, load_edges, transformer_edges = fetch_edges()
    print(
        f'  {len(transformer_edges)} transformer edge(s), '
        f'{len(line_edges)} line edge(s), '
        f'{len(load_edges)} load edge(s)\n'
    )

    sections = [
        ('# ── Transformer connections ────────────────────────────────────────────',
         gen_transformer_connections(transformer_edges)),
        ('# ── Line connections (LIM time-shifted) ────────────────────────────────',
         gen_line_connections(line_edges)),
        ('# ── Load connections ────────────────────────────────────────────────────',
         gen_load_connections(load_edges)),
        ('# ── Collector connections ───────────────────────────────────────────────',
         gen_collector_connections(transformer_edges, line_edges)),
    ]

    for header, code_lines in sections:
        print(header)
        print('\n'.join(code_lines))


# ============================================================================
# Step 3 -- adaptive NR simulation (from src/scenario_adaptive.py)
# ============================================================================

def run_simulation():
    # ── Fetch network data ─────────────────────────────────────────────────────────
    print('Fetching network parameters from GraphDB...')
    _sub_params, _transformers, _line_params, _load_params = fetch_all_network_params()

    # Flat start (V = nominal ∠0°) for the standard, unmodified IEEE 14 case only —
    # the PV/Battery/BatteryStorage variants keep the CIM SvVoltage warm start since
    # their NR solve is validated against that operating point.
    _SCHEME_NAME = _fetch_scheme_name()
    _FLAT_START  = _SCHEME_NAME.strip().lower() == 'ieee 14 feeder neplan cim'
    print(f'  Scheme: {_SCHEME_NAME!r}  →  flat start: {_FLAT_START}')

    _slack_subs        = {name for name, sp in _sub_params.items() if sp['is_slack']}
    _sync_machine_subs = {name for name, sp in _sub_params.items() if sp['is_sync_machine']}

    print('Fetching transformer edges ...')
    _, _, _tr_edges_pre = fetch_edges()
    # LV transformer buses are NOT forced as slack — the correct transformer model
    # (standard physical π-model with HV-referred impedance) allows them to be free
    # PQ nodes that naturally converge to the CIM operating point.
    _lv_slack_subs  = set()
    _all_slack_subs = _slack_subs | _lv_slack_subs
    if not _all_slack_subs:
        raise RuntimeError(
            'No slack bus found (no Substation with is_slack=True) — the NR power '
            'flow has no voltage/angle reference and cannot be solved.'
        )
    print(f'  True slack buses (is_slack=True): {sorted(_slack_subs)}')
    print(f'  SynchronousMachine buses:         {sorted(_sync_machine_subs)}')
    print(f'  LV transformer buses (treated as slack): {sorted(_lv_slack_subs)}')
    for _t in _transformers:
        print(f"  Transformer {_t['name']}: hv={_t.get('hv_nominal_voltage_kv')} kV "
              f"/ lv={_t.get('lv_nominal_voltage_kv')} kV")
    for (_fs, _ts), _lp in _line_params.items():
        print(f"  Line {_fs}→{_ts}: r={_lp['r_ohm']} Ω  x={_lp['x_ohm']} Ω  bch={_lp['bch']} S")

    # ── Detect optional elements ───────────────────────────────────────────────────
    _battery_buses = _fetch_device_buses('Battery')
    _pv_buses      = _fetch_device_buses('PV')
    _HAS_BATTERY   = len(_battery_buses) > 0
    _HAS_PV        = len(_pv_buses) > 0

    print(f'  Battery detected: {_HAS_BATTERY}  →  buses: {_battery_buses}')
    print(f'  PV detected:      {_HAS_PV}  →  buses: {_pv_buses}')

    # Use first detected bus; fall back to empty string (never reached when _HAS_x is False)
    _BATTERY_BUS      = _battery_buses[0] if _HAS_BATTERY else ''
    _BATTERY_P_CHARGE = 0.030   # Charge setpoint [MW] (positive = charge, 30 kW)
    # CIM PowerElectronicsConnection.controlMode=constantPowerFactor: P=30kW, Q=40kVAr → PF=0.6
    _BATTERY_PF       = _fetch_battery_power_factor() if _HAS_BATTERY else 1.0
    _PV_BUS           = _pv_buses[0] if _HAS_PV else ''
    _PV_CSV           = os.path.join(PROJECT_DIR, 'input_data.csv')
    _PV_SCALE_FACTOR  = 10.0   # Multiply FMU output: 10× ≈ 3.84 MW peak

    print('Fetching topology edges from GraphDB...')
    _line_edges, _load_edges, _tr_edges = fetch_edges()
    print(f'  {len(_tr_edges)} transformer, {len(_line_edges)} line, {len(_load_edges)} load edge(s)')

    def _tr_x_hv(t):
        """Return (r_hv, x_hv) referred to the HV side [Ω].
        CIM sometimes stores impedance on the LV winding — refer it to HV when needed.
        """
        x_hv = t.get('hv_x_ohm') or 0.0
        x_lv = t.get('lv_x_ohm') or 0.0
        r_hv = t.get('hv_r_ohm') or 0.0
        r_lv = t.get('lv_r_ohm') or 0.0
        if abs(x_hv) < 1e-12 and abs(x_lv) > 1e-12:
            u1 = t.get('hv_rated_u_kv') or t.get('hv_nominal_voltage_kv') or 1.0
            u2 = t.get('lv_rated_u_kv') or t.get('lv_nominal_voltage_kv') or 1.0
            ratio_sq = (u1 / u2) ** 2
            x_hv = x_lv * ratio_sq
            r_hv = r_lv * ratio_sq
        return r_hv, x_hv

    # ── Y_self per non-slack bus: lines + transformer branches ────────────────────
    _y_self = {}

    def _add_y(bus, y):
        if bus in _all_slack_subs:
            return
        re, im = _y_self.get(bus, (0.0, 0.0))
        _y_self[bus] = (re + y.real, im + y.imag)

    for _edge in _line_edges:
        _p = _line_params[(_edge['from_sub'], _edge['to_sub'])]
        _Z = complex(_p['r_ohm'], _p['x_ohm'])
        _add_y(_edge['from_sub'], 1.0 / _Z)
        _add_y(_edge['to_sub'],   1.0 / _Z)

    for _t in _transformers:
        _r_hv_t, _x_hv_t = _tr_x_hv(_t)
        _Z_tr = complex(_r_hv_t, _x_hv_t)
        if abs(_Z_tr) < 1e-15:
            continue
        _y_s = 1.0 / _Z_tr          # y_HV: HV-side series admittance
        _u1  = _t.get('hv_rated_u_kv') or _t.get('hv_nominal_voltage_kv') or 1.0
        _u2  = _t.get('lv_rated_u_kv') or _t.get('lv_nominal_voltage_kv') or 1.0
        _t_ratio = _u1 / _u2
        _hv_bus_t = next((_tr['hv_sub'] for _tr in _tr_edges_pre if _tr['tr_name'] == _t['name']), None)
        _lv_bus_t = next((_tr['lv_sub'] for _tr in _tr_edges_pre if _tr['tr_name'] == _t['name']), None)
        _hv_nom_t = _sub_params.get(_hv_bus_t, {}).get('v_nom_kv', 0.0) if _hv_bus_t else 0.0
        _lv_nom_t = _sub_params.get(_lv_bus_t, {}).get('v_nom_kv', 0.0) if _lv_bus_t else 0.0
        if _hv_bus_t and _lv_bus_t and _hv_nom_t < _lv_nom_t:
            _hv_bus_t, _lv_bus_t = _lv_bus_t, _hv_bus_t
        if _hv_bus_t:
            _add_y(_hv_bus_t, _y_s)                   # Y_self_HV += y_HV (correct)
        if _lv_bus_t:
            _add_y(_lv_bus_t, _y_s * _t_ratio**2)     # Y_self_LV += y_LV = t²·y_HV (correct)

    _all_line_subs  = sorted({e['from_sub'] for e in _line_edges} | {e['to_sub'] for e in _line_edges})
    _tr_hv_only_subs = {tr['hv_sub'] for tr in _tr_edges_pre} - set(_all_line_subs)
    _tr_lv_only_subs = {tr['lv_sub'] for tr in _tr_edges_pre} - set(_all_line_subs)
    _all_subs        = sorted(set(_all_line_subs) | _tr_hv_only_subs | _tr_lv_only_subs)
    _nonslock_subs   = [s for s in _all_subs if s not in _all_slack_subs]

    # Every simulated bus must have a path (via LINE or Transformer) back to a
    # slack bus — the NR solve has no defined voltage for a bus it can't reach.
    _adj = {}
    for _fs, _ts in ((e['from_sub'], e['to_sub']) for e in _line_edges):
        _adj.setdefault(_fs, set()).add(_ts)
        _adj.setdefault(_ts, set()).add(_fs)
    for _hv, _lv in ((tr['hv_sub'], tr['lv_sub']) for tr in _tr_edges_pre):
        if _hv and _lv:
            _adj.setdefault(_hv, set()).add(_lv)
            _adj.setdefault(_lv, set()).add(_hv)

    _reachable = set(_all_slack_subs)
    _frontier  = list(_all_slack_subs)
    while _frontier:
        _cur = _frontier.pop()
        for _nbr in _adj.get(_cur, ()):
            if _nbr not in _reachable:
                _reachable.add(_nbr)
                _frontier.append(_nbr)

    _unreachable_subs = sorted(set(_all_subs) - _reachable)
    if _unreachable_subs:
        raise RuntimeError(
            f'Bus(es) with no path to any slack bus — power flow is undefined for '
            f'them: {_unreachable_subs}'
        )

    N_LIM = 5  # Number of Gauss-Jacobi outer iterations per physical step (60 s)
    STOP  = 1440 * N_LIM   # 1440 time steps of 1 minutes in real world = 24 h

    # ── Simulator config (conditional on detected elements) ───────────────────────
    sim_config = {
        'TransformerBranch': {'python': 'transformer_branch_simulator:TransformerBranch'},
        'ACLineSegment':{'python': 'line_simulator:Line'},
        'SubstationNR': {'python': 'substation_nr_simulator:SubstationNR'},
        'Load':         {'python': 'load_simulator:Load'},
        'Collector':    {'python': 'collector:Collector'},
    }
    if _HAS_BATTERY:
        sim_config['Battery'] = {'python': 'battery_simulator:Battery'}
    if _HAS_PV:
        sim_config['PV']  = {'python': 'pv_simulator:PV'}
        sim_config['CSV'] = {'python': 'csv_reader_simulator:CSVReader'}

    world = mosaik.World(sim_config)

    # ── Transformer branch entities (π-model, step_size=1 = inner loop) ───────────
    tr_branch_sim = world.start('TransformerBranch',
        fmu_filename=os.path.join(FMU_DIR, 'TransformerBranch.fmu'),
        instance_name='TrBranch', step_size=1)

    _entity_map = {}
    for _t in _transformers:
        _r_hv_e, _x_hv_e = _tr_x_hv(_t)
        _u1_e = _t.get('hv_rated_u_kv') or _t.get('hv_nominal_voltage_kv') or 1.0
        _u2_e = _t.get('lv_rated_u_kv') or _t.get('lv_nominal_voltage_kv') or 1.0
        _e = tr_branch_sim.TransformerBranch.create(
            1, r_hv_ohm=_r_hv_e, x_hv_ohm=_x_hv_e,
            rated_u1_kv=_u1_e, rated_u2_kv=_u2_e,
        )[0]
        _entity_map[_tr_var(_t['name'])] = _e
        print(f'  TransformerBranch {_t["name"]}: U1={_u1_e} kV, U2={_u2_e} kV, '
              f'R={_r_hv_e:.6f} Ω, X={_x_hv_e:.6f} Ω')

    # ── Substations: SubstationNR FMU (NR inner solve, step_size=1) ───────────────
    bus_sim = world.start('SubstationNR',
        fmu_filename=os.path.join(FMU_DIR, 'SubstationNR.fmu'),
        instance_name='Substation_bus', step_size=1)

    for _sub_name in _all_subs:
        _sp = _sub_params.get(_sub_name, {'v_nom_kv': 20.0, 'is_slack': False, 'sv_voltage_kv': None})
        _v_slack  = _sp.get('sv_voltage_kv') or _sp['v_nom_kv']
        _is_slack = _sub_name in _all_slack_subs
        _is_sync  = _sub_name in _sync_machine_subs
        # NR warm-start seed for non-slack buses: CIM SvVoltage by default, or flat
        # (nominal ∠0°) for the standard IEEE 14 case. The slack bus itself always
        # keeps its true (fixed) value — flat start only changes the initial guess.
        if _FLAT_START and not _is_slack:
            _v_init, _ang_init = _sp['v_nom_kv'], 0.0
        else:
            _v_init, _ang_init = _v_slack, _sp.get('sv_angle_deg', 0.0) or 0.0
        if _is_slack:
            _e = bus_sim.SubstationNR.create(1, is_slack=1.0, V_slack_kv=_v_slack,
                                              V_slack_ang_deg=_sp.get('sv_angle_deg', 0.0))[0]
        elif _is_sync:
            _yre, _yim = _y_self.get(_sub_name, (0.0, 0.0))
            _e = bus_sim.SubstationNR.create(
                1, Y_self_re=_yre, Y_self_im=_yim, B_shunt=0.0, omega_relax=0.5,
                is_slack=0.0, V_slack_kv=_v_init,
                V_slack_ang_deg=_ang_init,
                is_sync_machine=1.0, V_reg_kv=_sp.get('sv_voltage_kv') or _sp['v_nom_kv'])[0]
        else:
            _yre, _yim = _y_self.get(_sub_name, (0.0, 0.0))
            _e = bus_sim.SubstationNR.create(
                1, Y_self_re=_yre, Y_self_im=_yim, B_shunt=0.0, omega_relax=0.5,
                is_slack=0.0, V_slack_kv=_v_init,
                V_slack_ang_deg=_ang_init)[0]
        _entity_map[_sub_var(_sub_name)] = _e
        print(f'  Substation {_sub_name}: is_slack={_is_slack}, is_sync={_is_sync}, '
              f'V_init={_v_init:.4f} kV @ {_ang_init:.4f}°')

    # ── Lines ──────────────────────────────────────────────────────────────────────
    line_sim = world.start('ACLineSegment',
        fmu_filename=os.path.join(FMU_DIR, 'ACLineSegment.fmu'),
        instance_name='Line', step_size=1)

    for _edge in _line_edges:
        _fs, _ts = _edge['from_sub'], _edge['to_sub']
        _p = _line_params[(_fs, _ts)]
        _e = line_sim.Line.create(1, r_ohm=_p['r_ohm'], x_ohm=_p['x_ohm'], bch=_p['bch'])[0]
        _entity_map[_line_var(_fs, _ts)] = _e

    # ── Loads ──────────────────────────────────────────────────────────────────────
    load_sim = world.start('Load',
        fmu_filename=os.path.join(FMU_DIR, 'Load.fmu'),
        instance_name='Load', step_size=N_LIM)

    for _load_name, _lp in _load_params.items():
        _e = load_sim.Load.create(1, p_mw=_lp['p_mw'], q_mvar=_lp['q_mvar'])[0]
        _entity_map[_load_var(_load_name)] = _e

    # SynchronousMachine generator injection loads (real power only; Q is FREE in PV-bus NR)
    # Sync machines use a 1-D polar NR that finds angle θ at |V|=V_reg satisfying P=P_sch.
    # Q_gen is implicitly solved — injecting a fixed Q from the CIM reference would force the
    # simulation to converge to a different operating point than the CIM PV-bus power flow.
    _SYNC_GEN_KEY = '__sync_gen__{}'
    for _sm in sorted(_sync_machine_subs):
        _sp  = _sub_params.get(_sm, {})
        _pg  = _sp.get('p_gen_mw') or 0.0
        if abs(_pg) < 1e-9:
            continue   # pure condenser (P_gen=0): no entity needed, NR handles Q freely
        _e = load_sim.Load.create(1, p_mw=-_pg, q_mvar=0.0)[0]   # Q=0; PV NR finds Q
        _entity_map[_SYNC_GEN_KEY.format(_sm)] = _e

    # ── Battery (conditional) ──────────────────────────────────────────────────────
    if _HAS_BATTERY:
        battery_sim = world.start('Battery',
            fmu_filename=os.path.join(FMU_DIR, 'Battery_Simulink_fmi3.fmu'),
            instance_name='Battery', step_size=N_LIM)
        _battery_e = battery_sim.Battery.create(1, p_charge_mw=_BATTERY_P_CHARGE, power_factor=_BATTERY_PF)[0]
        _entity_map['battery_1'] = _battery_e
        print(f'  Battery-1 at {_BATTERY_BUS} (p_charge={_BATTERY_P_CHARGE} MW, PF={_BATTERY_PF:.3f})')

    # ── PV + CSV weather (conditional) ────────────────────────────────────────────
    if _HAS_PV:
        csv_sim = world.start('CSV', step_size=N_LIM)
        _csv_e  = csv_sim.WeatherData.create(1, csv_file=_PV_CSV)[0]

        pv_sim = world.start('PV',
            fmu_filename=os.path.join(FMU_DIR, 'PV_Python_fmi2.fmu'),
            instance_name='PV_MPPT', step_size=N_LIM)
        _pv_e = pv_sim.PV.create(1, scale_factor=_PV_SCALE_FACTOR)[0]
        _entity_map['pv_1'] = _pv_e
        print(f'  PV-1 at {_PV_BUS} (scale={_PV_SCALE_FACTOR}x, weather={os.path.basename(_PV_CSV)})')

    # ── Collector ─────────────────────────────────────────────────────────────────
    collector_sim = world.start('Collector', output_dir=PROJECT_DIR, total_steps=STOP)
    collector = collector_sim.Monitor()

    # ── Connections ────────────────────────────────────────────────────────────────
    # Transformer branches: HV + LV bus voltages (time_shifted) → current injections
    for _tr in _tr_edges:
        _tr_e  = _entity_map[_tr_var(_tr['tr_name'])]
        _db_hv = _tr['hv_sub']
        _db_lv = _tr['lv_sub']
        _db_hv_nom = _sub_params.get(_db_hv, {}).get('v_nom_kv', 0.0)
        _db_lv_nom = _sub_params.get(_db_lv, {}).get('v_nom_kv', 0.0)
        if _db_hv_nom < _db_lv_nom:
            _db_hv, _db_lv = _db_lv, _db_hv
        _hv_e = _entity_map[_sub_var(_db_hv)]
        _lv_e = _entity_map[_sub_var(_db_lv)]
        _v_init_hv = _sub_params.get(_db_hv, {}).get('sv_voltage_kv') or _sub_params.get(_db_hv, {}).get('v_nom_kv', 69.0)
        _v_init_lv = _sub_params.get(_db_lv, {}).get('sv_voltage_kv') or _sub_params.get(_db_lv, {}).get('v_nom_kv', 13.8)
        _ang_hv = _sub_params.get(_db_hv, {}).get('sv_angle_deg', 0.0) or 0.0
        _ang_lv = _sub_params.get(_db_lv, {}).get('sv_angle_deg', 0.0) or 0.0
        world.connect(_hv_e, _tr_e, ('V_mag_kv', 'V_hv_mag_kv'),
                      time_shifted=True, initial_data={'V_mag_kv': _v_init_hv})
        world.connect(_hv_e, _tr_e, ('V_ang_deg', 'V_hv_ang_deg'),
                      time_shifted=True, initial_data={'V_ang_deg': _ang_hv})
        world.connect(_lv_e, _tr_e, ('V_mag_kv', 'V_lv_mag_kv'),
                      time_shifted=True, initial_data={'V_mag_kv': _v_init_lv})
        world.connect(_lv_e, _tr_e, ('V_ang_deg', 'V_lv_ang_deg'),
                      time_shifted=True, initial_data={'V_ang_deg': _ang_lv})
        world.connect(_tr_e, _hv_e, ('I_hv_in_re', 'I_in_re'))
        world.connect(_tr_e, _hv_e, ('I_hv_in_im', 'I_in_im'))
        world.connect(_tr_e, _lv_e, ('I_lv_in_re', 'I_in_re'))
        world.connect(_tr_e, _lv_e, ('I_lv_in_im', 'I_in_im'))

    # Lines: time-shifted voltage inputs + current injections (Gauss-Jacobi)
    for _edge in _line_edges:
        _fs, _ts = _edge['from_sub'], _edge['to_sub']
        _from_e = _entity_map[_sub_var(_fs)]
        _to_e   = _entity_map[_sub_var(_ts)]
        _line_e = _entity_map[_line_var(_fs, _ts)]
        _vf = (_sub_params.get(_fs, {}).get('sv_voltage_kv') or
                _sub_params.get(_fs, {}).get('v_nom_kv', 20.0))
        _vt = (_sub_params.get(_ts, {}).get('sv_voltage_kv') or
                _sub_params.get(_ts, {}).get('v_nom_kv', 20.0))
        _af = _sub_params.get(_fs, {}).get('sv_angle_deg', 0.0) or 0.0
        _at = _sub_params.get(_ts, {}).get('sv_angle_deg', 0.0) or 0.0
        world.connect(_from_e, _line_e, ('V_mag_kv', 'V_from_mag_kv'), time_shifted=True, initial_data={'V_mag_kv': _vf})
        world.connect(_from_e, _line_e, ('V_ang_deg', 'V_from_ang_deg'), time_shifted=True, initial_data={'V_ang_deg': _af})
        world.connect(_to_e,   _line_e, ('V_mag_kv', 'V_to_mag_kv'),   time_shifted=True, initial_data={'V_mag_kv': _vt})
        world.connect(_to_e,   _line_e, ('V_ang_deg', 'V_to_ang_deg'), time_shifted=True, initial_data={'V_ang_deg': _at})
        world.connect(_line_e, _to_e,   ('I_to_re',       'I_in_re'))
        world.connect(_line_e, _to_e,   ('I_to_im',       'I_in_im'))
        world.connect(_line_e, _from_e, ('I_neg_from_re', 'I_in_re'))
        world.connect(_line_e, _from_e, ('I_neg_from_im', 'I_in_im'))

    # Loads → substations
    for _edge in _load_edges:
        world.connect(_entity_map[_load_var(_edge['load_name'])],
                      _entity_map[_sub_var(_edge['sub_name'])],
                      ('P_load_mw', 'P_load_mw'), ('Q_load_mvar', 'Q_load_mvar'))

    # SynchronousMachine injection loads → buses
    for _sm in sorted(_sync_machine_subs):
        _key = _SYNC_GEN_KEY.format(_sm)
        if _key not in _entity_map:
            continue
        world.connect(_entity_map[_key], _entity_map[_sub_var(_sm)],
                      ('P_load_mw', 'P_load_mw'), ('Q_load_mvar', 'Q_load_mvar'))

    # Battery → bus (conditional)
    if _HAS_BATTERY:
        world.connect(_entity_map['battery_1'],
                      _entity_map[_sub_var(_BATTERY_BUS)],
                      ('P_load_mw', 'P_load_mw'), ('Q_load_mvar', 'Q_load_mvar'))

    # PV → bus via CSV weather (conditional)
    if _HAS_PV:
        world.connect(_csv_e, _pv_e, ('S', 'S'), ('T', 'T'))
        world.connect(_entity_map['pv_1'],
                      _entity_map[_sub_var(_PV_BUS)],
                      ('P_load_mw', 'P_load_mw'))

    # ── Collector connections ──────────────────────────────────────────────────────
    for _sub_name in _all_subs:
        _sv = _sub_var(_sub_name)
        world.connect(_entity_map[_sv], collector,
                      ('V_mag_kv', f'V_{_sv}_mag_kv'),
                      ('V_ang_deg', f'V_{_sv}_ang_deg'))

    for _edge in _line_edges:
        _lv = _line_var(_edge['from_sub'], _edge['to_sub'])
        world.connect(_entity_map[_lv], collector,
                      ('P_loss_mw',   f'P_loss_{_lv}_mw'),
                      ('Q_loss_mvar', f'Q_loss_{_lv}_mvar'),
                      ('I_from_mag_kA', f'I_from_{_lv}_kA'),
                      ('I_to_mag_kA',   f'I_to_{_lv}_kA'))

    for _t in _transformers:
        _tv = _tr_var(_t['name'])
        world.connect(_entity_map[_tv], collector,
                      ('P_loss_mw',   f'P_loss_{_tv}_mw'),
                      ('Q_loss_mvar', f'Q_loss_{_tv}_mvar'),
                      ('I_hv_mag_kA', f'I_hv_mag_{_tv}_kA'))

    if _HAS_BATTERY:
        world.connect(_entity_map['battery_1'], collector,
                      ('SOC',         'Battery1_SOC'),
                      ('P_load_mw',   'Battery1_P_load_mw'),
                      ('Q_load_mvar', 'Battery1_Q_load_mvar'),
                      ('V_volt',      'Battery1_V_volt'),
                      ('I_amp',       'Battery1_I_amp'))

    if _HAS_PV:
        world.connect(_entity_map['pv_1'], collector,
                      ('P_load_mw', 'PV1_P_load_mw'),
                      ('P',         'PV1_P_W'),
                      ('V',         'PV1_V_volt'),
                      ('I',         'PV1_I_amp'))

    # ── Live dashboard (auto-regenerates graph.html from Neo4j) ───────────────────
    _graph_html  = os.path.join(PROJECT_DIR, 'graph.html')
    _dashboard   = os.path.join(PROJECT_DIR, 'live_dashboard.html')
    _status_path = os.path.join(PROJECT_DIR, 'sim_status.json')

    with open(_status_path, 'w') as _f:
        json.dump({'running': True, 'step': 0, 'total': STOP}, _f)

    try:
        import visualize as _viz  # type: ignore[import-untyped]
        _nodes, _edges, _scheme = _viz.fetch_graph()
        _net = _viz.build_html_graph(_nodes, _edges)
        _net.save_graph(_graph_html)
        _viz.inject_legend(_graph_html)
        _viz.export_topology_json(_nodes, _edges,
                                  os.path.join(PROJECT_DIR, 'topology_export.json'),
                                  scheme_name=_scheme)
        print(f'[dashboard] Graph regenerated: {len(_nodes)} nodes, scheme={_scheme!r}')
    except Exception as _e:
        print(f'[dashboard] Warning: could not regenerate graph.html: {_e}')

    if generate_live_dashboard(_graph_html, _dashboard):
        _server, _url = start_live_server(PROJECT_DIR, port=8765)
        if _server:
            print(f'\n{"="*60}')
            print(f'  Live dashboard: {_url}')
            print(f'  Opening in browser...')
            print(f'{"="*60}\n')
            webbrowser.open(_url)
        else:
            print(f'\n  Live dashboard generated: {_dashboard}\n')
    else:
        print('\n  [live_server] graph.html not found; skipping live dashboard.\n')

    # ── Run ────────────────────────────────────────────────────────────────────────
    world.run(until=STOP)

    # Save a named copy for cross-scenario comparison
    shutil.copy(os.path.join(PROJECT_DIR, 'output.json'),
                os.path.join(PROJECT_DIR, 'output_adaptive.json'))
    print('Saved output_adaptive.json')

    # ── Post-simulation visualization ─────────────────────────────────────────────
    print('\n=== Generating visualization ===')
    try:
        with open(os.path.join(PROJECT_DIR, 'output.json'), 'r') as f:
            data = json.load(f)

        def series(key):
            if key not in data:
                return [], []
            d    = data[key]
            times = sorted(d.keys(), key=int)
            phys_t, vals = [], []
            for t_str in times:
                t = int(t_str)
                if (t + 1) % N_LIM == 0:
                    phys_t.append((t // N_LIM) * 60)
                    vals.append(d[t_str])
            if not phys_t:
                phys_t = [(int(t) // N_LIM) * 60 for t in times]
                vals   = [d[t] for t in times]
            return phys_t, vals

        # ── Build subplot list dynamically ────────────────────────────────────────
        _subplot_titles = [
            '|V| Bus Voltages (kV)',
            'Voltage Angle (deg)',
            'Line Active Power Losses (MW)',
            'Branch Current Magnitudes (kA)',
        ]
        if _HAS_BATTERY:
            _subplot_titles += [
                'Battery: State of Charge (%)',
                'Battery: Terminal Voltage (V)',
                'Battery: Charge / Discharge Power (MW)',
            ]
        if _HAS_PV:
            _subplot_titles += [
                'PV: Active Power (MW)',
                'PV: Terminal Voltage (V)',
            ]

        _n_rows = len(_subplot_titles)
        fig = make_subplots(rows=_n_rows, cols=1, shared_xaxes=True,
                            subplot_titles=_subplot_titles)

        # Row 1: bus voltage magnitudes
        for _sub_name in _all_subs:
            _sv = _sub_var(_sub_name)
            t, v = series(f'V_{_sv}_mag_kv')
            fig.add_trace(go.Scatter(x=t, y=v, name=f'|V| {_sub_name}'), row=1, col=1)

        # Row 2: voltage angles (non-slack)
        for _sub_name in _nonslock_subs:
            _sv = _sub_var(_sub_name)
            t, v = series(f'V_{_sv}_ang_deg')
            fig.add_trace(go.Scatter(x=t, y=v, name=f'∠ {_sub_name}',
                                     line=dict(dash='dot')), row=2, col=1)

        # Row 3: line losses
        for _edge in _line_edges:
            _lv = _line_var(_edge['from_sub'], _edge['to_sub'])
            t, v = series(f'P_loss_{_lv}_mw')
            fig.add_trace(go.Scatter(x=t, y=v,
                                     name=f'{_edge["from_sub"]}→{_edge["to_sub"]}',
                                     line=dict(dash='dash')), row=3, col=1)

        # Row 4: branch current magnitudes
        for _edge in _line_edges:
            _lv = _line_var(_edge['from_sub'], _edge['to_sub'])
            t, v = series(f'I_from_{_lv}_kA')
            fig.add_trace(go.Scatter(x=t, y=v,
                                     name=f'{_edge["from_sub"]}→{_edge["to_sub"]}'), row=4, col=1)

        _row = 5
        if _HAS_BATTERY:
            t, v = series('Battery1_SOC')
            if t:
                fig.add_trace(go.Scatter(x=t, y=v, name='SOC (%)',
                                         line=dict(color='green')), row=_row, col=1)
            t, v = series('Battery1_V_volt')
            if t:
                fig.add_trace(go.Scatter(x=t, y=v, name='Voltage (V)',
                                         line=dict(color='orange')), row=_row + 1, col=1)
            t, v = series('Battery1_P_load_mw')
            if t:
                fig.add_trace(go.Scatter(x=t, y=[max(p, 0) for p in v], name='Charge (MW)',
                                         line=dict(color='blue')), row=_row + 2, col=1)
                fig.add_trace(go.Scatter(x=t, y=[abs(min(p, 0)) for p in v], name='Discharge (MW)',
                                         line=dict(color='red', dash='dot')), row=_row + 2, col=1)
            _row += 3

        if _HAS_PV:
            t, v = series('PV1_P_load_mw')
            if t:
                fig.add_trace(go.Scatter(x=t, y=[-p for p in v], name='P_gen (MW)',
                                         line=dict(color='#F39C12')), row=_row, col=1)
            t, v = series('PV1_V_volt')
            if t:
                fig.add_trace(go.Scatter(x=t, y=v, name='V (V)',
                                         line=dict(color='#E74C3C')), row=_row + 1, col=1)

        fig.update_xaxes(title_text='Physical time (s)', row=_n_rows, col=1)
        fig.update_yaxes(title_text='|V| (kV)',    row=1, col=1)
        fig.update_yaxes(title_text='angle (deg)', row=2, col=1)
        fig.update_yaxes(title_text='P_loss (MW)', row=3, col=1)
        fig.update_yaxes(title_text='|I| (kA)',    row=4, col=1)

        _scheme_label = _scheme if '_scheme' in dir() else 'Adaptive'
        fig.update_layout(
            title_text=f'Adaptive NR Co-simulation: {_scheme_label} — 24h quasi-static',
            hovermode='x unified',
        )
        out_html = os.path.join(PROJECT_DIR, 'output_NR.html')
        fig.write_html(out_html)
        print(f'Visualization saved to {out_html}')

    except FileNotFoundError:
        print('Warning: output.json not found.')
    except Exception as e:
        import traceback
        print(f'Visualization error: {e}')
        traceback.print_exc()


# ============================================================================
# Entry point
# ============================================================================

def main():
    if len(sys.argv) < 2:
        print("Usage: python scenario_complete.py <cim_file_or_folder>")
        sys.exit(1)
    cim_source = sys.argv[1]

    print(f"\n{'=' * 70}\nSTEP 1/3 -- CIM ingestion\n{'=' * 70}")
    ingest(cim_source)

    print(f"\n{'=' * 70}\nSTEP 2/3 -- Topology preview\n{'=' * 70}")
    preview_connections()

    print(f"\n{'=' * 70}\nSTEP 3/3 -- Adaptive NR simulation\n{'=' * 70}")
    run_simulation()


if __name__ == '__main__':
    main()
