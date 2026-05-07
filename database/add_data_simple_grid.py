"""
Extract simple grid substations N0-N2, ACLineSegments and PowerTransformers from
CGMES XML files in cim_generated/ and push them into a Neo4j Aura graph database.

Graph shape:
  (Ni:Substation)-[:LINE {all ACLineSegment properties}]->(Nj:Substation)
  (Nk:Substation)-[:CONNECT_TO {terminal_id, side}]->(TRx:Transformer)-[:CONNECT_TO {terminal_id, side}]->(Nl:Substation)
  (Nm:Substation)-[:CONNECT_TO {terminal_id}]->(Lx:Load)

Excluded: SvVoltage (voltage magnitude and phase angle from StateVariables).
"""

import os
import re
from xml.etree.ElementTree import parse as et_parse

from dotenv import load_dotenv
from neo4j import GraphDatabase

load_dotenv()

NEO4J_URI = os.getenv("NEO4J_URI")
NEO4J_USERNAME = os.getenv("NEO4J_USERNAME")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD")
NEO4J_DATABASE = os.getenv("NEO4J_DATABASE")

EQ_FILE = "cim_generated_3/simple_grid_3_cgmes_v2_4_15_Equipment.xml"
TP_FILE = "cim_generated_3/simple_grid_3_cgmes_v2_4_15_Topology.xml"
SV_FILE = "cim_generated_3/simple_grid_3_cgmes_v2_4_15_StateVariables.xml"

RDF_NS = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
CIM_NS = "http://iec.ch/TC57/2012/CIM-schema-cim16#"

# Substation names to include
SUBSTATION_NAMES = {"N0", "N1", "N2"}


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

    for elem in root:
        tag = elem.tag
        rid = _rdf_id(elem)

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
            if name not in SUBSTATION_NAMES:
                continue
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

        elif tag == f"{{{CIM_NS}}}EnergyConsumer":
            loads[rid] = {
                "name": _text(elem, "IdentifiedObject.name"),
                "rdf_id": rid,
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
            seq = _text(elem, "ACDCTerminal.sequenceNumber")
            if ce_id:
                terminals[rid] = {
                    "equipment_id": ce_id,
                    "seq": int(seq) if seq else None,
                }

    # Resolve nominal voltage for substations via their voltage levels
    for vl in voltage_levels.values():
        sub_id = vl["substation_id"]
        bv_id = vl["base_voltage_id"]
        if sub_id in substations and bv_id in base_voltages:
            substations[sub_id]["nominal_voltage_kv"] = base_voltages[bv_id]

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

    # Remove None-valued properties
    substations = {k: {pk: pv for pk, pv in v.items() if pv is not None} for k, v in substations.items()}
    lines = {k: {pk: pv for pk, pv in v.items() if pv is not None} for k, v in lines.items()}
    transformers = {k: {pk: pv for pk, pv in v.items() if pv is not None} for k, v in transformers.items()}

    return substations, lines, terminals, transformers, transformer_ends, loads


def parse_state_variables():
    """
    Parse StateVariables XML and return:
      sv_flows : dict  terminal_id -> {p_mw, q_mvar}
    """
    root = et_parse(SV_FILE).getroot()
    sv_flows = {}

    for elem in root:
        if elem.tag != f"{{{CIM_NS}}}SvPowerFlow":
            continue
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

    return sv_flows


def parse_topology():
    """
    Return mapping: terminal_id -> topological_node_name (e.g. 'N1').
    Built from TopologicalNode.Terminal listings in the Topology XML.
    """
    root = et_parse(TP_FILE).getroot()
    terminal_to_node = {}

    for elem in root:
        if elem.tag != f"{{{CIM_NS}}}TopologicalNode":
            continue
        node_name = _text(elem, "IdentifiedObject.name")
        for t_ref in elem.findall(f"{{{CIM_NS}}}TopologicalNode.Terminal"):
            tid = t_ref.get(f"{{{RDF_NS}}}resource", "").lstrip("#")
            terminal_to_node[tid] = node_name

    return terminal_to_node


def build_load_connections(loads, terminals, terminal_to_node, sv_flows):
    """
    For each EnergyConsumer (load), find the connected substation node and
    attach SvPowerFlow p/q values.

    Returns a list of (load_id, load_props_dict, node_name, terminal_id).
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
        node = terminal_to_node.get(tid)
        if node is None:
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
        connections.append((load_id, load_props, node, tid))

    return connections


def build_connections(lines, terminals, terminal_to_node):
    """
    For each ACLineSegment, find the substation at each end using terminal
    sequence numbers (1 = from-side, 2 = to-side).

    Returns a list of (line_id, from_node_name, from_tid, to_node_name, to_tid).
    """
    endpoints = {}
    for term_id, term in terminals.items():
        eq_id = term["equipment_id"]
        if eq_id not in lines:
            continue
        node = terminal_to_node.get(term_id)
        if node is None:
            continue
        endpoints.setdefault(eq_id, {})[term["seq"]] = (node, term_id)

    connections = []
    for line_id, ep in endpoints.items():
        from_node, from_tid = ep.get(1, (None, None))
        to_node, to_tid = ep.get(2, (None, None))
        connections.append((line_id, from_node, from_tid, to_node, to_tid))

    return connections


def build_transformer_connections(transformers, transformer_ends, terminals, terminal_to_node):
    """
    For each PowerTransformer, find the substation at each end.

    Returns a list of (tr_id, hv_node_name, hv_tid, lv_node_name, lv_tid).
    """
    endpoints = {}
    for end in transformer_ends.values():
        tr_id = end["transformer_id"]
        if tr_id not in transformers:
            continue
        tid = end["terminal_id"]
        node = terminal_to_node.get(tid)
        if node is None:
            continue
        endpoints.setdefault(tr_id, {})[end["end_number"]] = (node, tid)

    connections = []
    for tr_id, ep in endpoints.items():
        hv_node, hv_tid = ep.get(1, (None, None))
        lv_node, lv_tid = ep.get(2, (None, None))
        connections.append((tr_id, hv_node, hv_tid, lv_node, lv_tid))

    return connections


def push_to_neo4j(substations, lines, connections, transformers, tr_connections, loads, load_connections):
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USERNAME, NEO4J_PASSWORD))

    with driver.session(database=NEO4J_DATABASE) as session:
        # Clear existing data
        session.execute_write(lambda tx: tx.run("MATCH (n) DETACH DELETE n"))
        print("  Cleared existing graph.")

        # Batch-create Substation nodes
        session.execute_write(
            lambda tx: tx.run(
                "UNWIND $rows AS props CREATE (n:Substation) SET n = props",
                rows=list(substations.values()),
            )
        )
        print(f"  Created {len(substations)} Substation nodes.")

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
                MATCH (a:Substation {name: row.from_node})
                MATCH (b:Substation {name: row.to_node})
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
                MATCH (s:Substation {name: row.node})
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
                MATCH (s:Substation {name: row.node})
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
                MATCH (s:Substation {name: row.node})
                MATCH (l:Load {rdf_id: row.rdf_id})
                CREATE (s)-[:CONNECT_TO {terminal_id: row.tid}]->(l)
                """,
                rows=load_rows,
            )
        )
        print(f"  Created {len(load_rows)} Load CONNECT_TO relationships.")

    driver.close()


if __name__ == "__main__":
    print("Parsing Equipment XML …")
    substations, lines, terminals, transformers, transformer_ends, loads = parse_equipment()
    print(f"  {len(substations)} substations, {len(lines)} ACLineSegments, "
          f"{len(transformers)} transformers, {len(terminals)} terminals, {len(loads)} loads")

    print("Parsing Topology XML …")
    terminal_to_node = parse_topology()
    print(f"  {len(terminal_to_node)} terminal→node mappings")

    print("Parsing StateVariables XML …")
    sv_flows = parse_state_variables()
    print(f"  {len(sv_flows)} SvPowerFlow records")

    print("Building line connections …")
    connections = build_connections(lines, terminals, terminal_to_node)
    print(f"  {len(connections)} ACLineSegment connections")

    print("Building transformer connections …")
    tr_connections = build_transformer_connections(transformers, transformer_ends, terminals, terminal_to_node)
    print(f"  {len(tr_connections)} transformer connections")

    print("Building load connections …")
    load_connections = build_load_connections(loads, terminals, terminal_to_node, sv_flows)
    print(f"  {len(load_connections)} load connections")

    print("Pushing to Neo4j Aura …")
    push_to_neo4j(substations, lines, connections, transformers, tr_connections, loads, load_connections)
    print("Done.")
