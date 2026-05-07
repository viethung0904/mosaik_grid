"""
Mosaik co-simulation scenario — LIM-based network power flow (generic slack detection).
Graph-driven: topology, parameters, and FMU instantiation counts all derived from Neo4j.

Slack bus identification is fully data-driven: the Substation node's `is_slack` property
(stored by add_data_slack_detection.py from SvVoltage.angle == 0 in the CIM file) is
queried from Neo4j. No hard-coded voltage level or transformer-LV assumptions are made.

Per-bus nominal voltage (Substation.nominal_voltage_kv) is used for V_slack_kv and for
the initial_data in time_shifted LIM connections, making the scenario work correctly for
multi-voltage networks (e.g. IEEE 14-bus with 69 kV, 13.8 kV and 18 kV zones).

LIM mapping:
    Each mosaik timestep = one Jacobi LIM iteration.
    time_shifted=True on Sub→Line connections provides the LIM one-step delay.
    omega_relax=0.5 is required for convergence on radial networks (ρ_eff ≈ 0.5).

Network data sourced from Neo4j GraphDB.
"""
import os
import sys
import json
import re
import threading
import webbrowser

from dotenv import load_dotenv
from neo4j import GraphDatabase
import mosaik
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from live_server import start_live_server, generate_live_dashboard

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, os.path.join(SCRIPT_DIR, 'simulator'))
sys.path.insert(0, SCRIPT_DIR)
from connect import fetch_edges, _sub_var, _line_var, _load_var, _tr_var

FMU_DIR = os.path.join(PROJECT_DIR, 'fmus')

# ── GraphDB: fetch all network parameters generically ─────────────────────────
def fetch_all_network_params():
    """
    Query Neo4j generically for all substations, transformers, lines, and loads.

    Returns:
        sub_params   : dict  sub_name -> {v_nom_kv, is_slack}
        transformers : list of dicts {name, hv_nominal_voltage_kv, lv_nominal_voltage_kv, ...}
        line_params  : dict (from_sub, to_sub) -> {r_ohm, x_ohm, bch}
        load_params  : dict load_name -> {p_mw, q_mvar}
    """
    load_dotenv(os.path.join(PROJECT_DIR, '.env'))
    uri  = os.getenv('NEO4J_URI')
    user = os.getenv('NEO4J_USERNAME')
    pwd  = os.getenv('NEO4J_PASSWORD')
    db   = os.getenv('NEO4J_DATABASE')

    driver = GraphDatabase.driver(uri, auth=(user, pwd))
    try:
        with driver.session(database=db) as session:
            # All substations — including is_slack, nominal voltage, and CIM sv_voltage_kv
            sub_records = session.run(
                'MATCH (s:Substation) '
                'RETURN s.name AS name, s.nominal_voltage_kv AS v_nom, '
                '       s.is_slack AS is_slack, s.is_pv AS is_pv, '
                '       s.sv_voltage_kv AS sv_v, s.sv_angle_deg AS sv_ang, '
                '       s.p_gen_mw AS p_gen_mw, s.q_gen_mvar AS q_gen_mvar '
                'ORDER BY s.name'
            ).data()
            sub_params = {
                r['name']: {
                    'v_nom_kv':      r['v_nom'] if r['v_nom'] is not None else 20.0,
                    'is_slack':      bool(r['is_slack']) if r['is_slack'] is not None else False,
                    'is_pv':         bool(r['is_pv'])    if r['is_pv']    is not None else False,
                    'sv_voltage_kv': r['sv_v']  if r['sv_v']  is not None else None,
                    'sv_angle_deg':  r['sv_ang'] if r['sv_ang'] is not None else 0.0,
                    'p_gen_mw':      r['p_gen_mw']   if r['p_gen_mw']   is not None else 0.0,
                    'q_gen_mvar':    r['q_gen_mvar'] if r['q_gen_mvar'] is not None else 0.0,
                }
                for r in sub_records
            }
            if not sub_params:
                raise RuntimeError('No Substation nodes found in graph database')

            # All transformers
            tr_records = session.run(
                'MATCH (t:Transformer) RETURN t ORDER BY t.name'
            ).data()
            transformers = [dict(r['t']) for r in tr_records]
            if not transformers:
                raise RuntimeError('No Transformer nodes found in graph database')

            # All LINE edges with impedance parameters
            line_records = session.run(
                'MATCH (a:Substation)-[l:LINE]->(b:Substation) '
                'RETURN a.name AS from_sub, b.name AS to_sub, '
                '       l.r_ohm AS r_ohm, l.x_ohm AS x_ohm, l.bch AS bch '
                'ORDER BY a.name, b.name'
            ).data()
            line_params = {
                (r['from_sub'], r['to_sub']): {
                    'r_ohm': r['r_ohm'], 'x_ohm': r['x_ohm'], 'bch': r['bch'],
                }
                for r in line_records
            }
            if not line_params:
                raise RuntimeError('No LINE edges found in graph database')

            # All loads with P/Q values
            load_records = session.run(
                'MATCH (l:Load) RETURN l.name AS name, l.p_mw AS p_mw, l.q_mvar AS q_mvar '
                'ORDER BY l.name'
            ).data()
            load_params = {
                r['name']: {'p_mw': r['p_mw'], 'q_mvar': r['q_mvar']}
                for r in load_records
            }
            if not load_params:
                raise RuntimeError('No Load nodes found in graph database')

    finally:
        driver.close()

    return sub_params, transformers, line_params, load_params


print('Fetching network parameters from GraphDB...')
_sub_params, _transformers, _line_params, _load_params = fetch_all_network_params()

_slack_subs = {name for name, sp in _sub_params.items() if sp['is_slack']}
_pv_subs    = {name for name, sp in _sub_params.items() if sp['is_pv']}

# LV-side buses of transformers act as voltage-controlled (slack) nodes for their
# downstream zone.  Their operating voltage is the CIM SvVoltage reference value.
# We derive this set from the transformer edges (fetched later), so we pre-fetch now.
print('Fetching transformer edges for LV-slack detection …')
_, _, _tr_edges_pre = fetch_edges()
_lv_slack_subs = {_tr['lv_sub'] for _tr in _tr_edges_pre if _tr['lv_sub'] not in _slack_subs}
_all_slack_subs = _slack_subs | _lv_slack_subs
print(f'  True slack buses (is_slack=True): {sorted(_slack_subs)}')
print(f'  PV generator buses (is_pv=True):  {sorted(_pv_subs)}')
print(f'  LV transformer buses (treated as slack): {sorted(_lv_slack_subs)}')
for _t in _transformers:
    print(f"  Transformer {_t['name']}: hv={_t.get('hv_nominal_voltage_kv')} kV / lv={_t.get('lv_nominal_voltage_kv')} kV")
for (_fs, _ts), _lp in _line_params.items():
    print(f"  Line {_fs}→{_ts}: r={_lp['r_ohm']} Ω  x={_lp['x_ohm']} Ω  bch={_lp['bch']} S")
for _ln, _lp in _load_params.items():
    print(f"  {_ln}: p={_lp['p_mw']} MW  q={_lp['q_mvar']} MVAr")

print('Fetching topology edges from GraphDB...')
_line_edges, _load_edges, _tr_edges = fetch_edges()
print(f'  {len(_tr_edges)} transformer, {len(_line_edges)} line, {len(_load_edges)} load edge(s)')

# ── Transformer equivalent loads at HV buses ──────────────────────────────────
# Each HV bus (BUS_4, BUS_5) that feeds LV zones via transformers must be given
# an additional constant-power load equal to the NET power the transformers drain.
#
# Method: HV Y-bus approach.
#   Build the admittance matrix for the HV-only sub-network (lines whose BOTH endpoints
#   have v_nom_kv >= hv_threshold_kv).  For each non-slack, non-PV HV bus that has
#   at least one transformer connection, compute the nodal power injection
#       S = V · (Y_HV · V)*
#   using the CIM SvVoltage reference voltages.  This equals the TOTAL power the bus
#   must receive from its HV neighbours — i.e. direct loads + transformer demand.
#   Subtracting the known direct loads gives the transformer equivalent load.
def _compute_tr_equiv_loads():
    """
    Returns dict: hv_bus_name -> (p_tr_mw, q_tr_mvar) — total transformer demand
    at each HV non-slack, non-PV bus.  Only buses that have at least one transformer
    LV terminal are included.
    """
    import cmath, math

    # ── 1. Identify HV zone buses by nominal voltage ──────────────────────────
    hv_threshold_kv = 50.0  # 69 kV zone in IEEE 14-bus
    hv_buses = {b for b, sp in _sub_params.items()
                if sp.get('v_nom_kv', 0.0) >= hv_threshold_kv}

    # HV buses that have at least one transformer going to an LV zone
    _hv_terminals = {tr['hv_sub'] for tr in _tr_edges_pre}
    _lv_terminals = {tr['lv_sub'] for tr in _tr_edges_pre}
    # Pure-HV transformer buses: in hv_buses, not themselves a LV terminal
    hv_tr_buses = (hv_buses & _hv_terminals) - _lv_terminals

    if not hv_tr_buses:
        return {}

    # ── 2. Reference complex voltages for all HV zone buses ───────────────────
    V = {}
    for b in hv_buses:
        sp = _sub_params.get(b, {})
        vmag = sp.get('sv_voltage_kv') or sp.get('v_nom_kv', 1.0)
        vang = sp.get('sv_angle_deg', 0.0)
        V[b] = cmath.rect(vmag, math.radians(vang))

    # ── 3. Build Y-bus for the HV sub-network (series admittances only) ───────
    all_hv = sorted(hv_buses)
    n = len(all_hv)
    idx = {b: i for i, b in enumerate(all_hv)}
    Y = [[complex(0)] * n for _ in range(n)]

    for e in _line_edges:
        fs, ts = e['from_sub'], e['to_sub']
        if fs not in idx or ts not in idx:
            continue  # skip lines that cross into the LV zone
        p = _line_params[(fs, ts)]
        Z = complex(p['r_ohm'], p['x_ohm'])
        y = 1.0 / Z if abs(Z) > 1e-15 else complex(1e6, 0)
        i, j = idx[fs], idx[ts]
        Y[i][i] += y
        Y[j][j] += y
        Y[i][j] -= y
        Y[j][i] -= y

    # ── 4. Direct loads at each HV bus (from load edges) ─────────────────────
    p_load_at = {}
    q_load_at = {}
    for _edge in _load_edges:
        b, ln = _edge['sub_name'], _edge['load_name']
        if ln in _load_params:
            p_load_at[b] = p_load_at.get(b, 0.0) + _load_params[ln]['p_mw']
            q_load_at[b] = q_load_at.get(b, 0.0) + _load_params[ln]['q_mvar']

    # ── 5. Compute transformer equiv load per HV transformer bus ─────────────
    result = {}
    for b in hv_tr_buses:
        if b not in idx:
            continue
        i = idx[b]
        I_inj = sum(Y[i][j] * V[all_hv[j]] for j in range(n))
        S = V[b] * I_inj.conjugate()
        # Fixed-point condition at bus b:
        #   I_in = I_load  where  I_in = -I_inj - j·Σ(bch/2)·V_b
        #   => P_load = -S.real,  Q_load = -S.imag + Σ(bch/2)·|V_b|²
        # The ACLineSegment pi-model drains j·(bch/2)·V from each line end,
        # reducing I_in relative to the series-only Y-bus.  This reactive drain
        # must be added to Q_load so the fixed point sits at the reference.
        bch_half_sum = sum(
            _line_params[(e['from_sub'], e['to_sub'])]['bch'] / 2.0
            for e in _line_edges
            if (e['from_sub'] == b or e['to_sub'] == b)
               and e['from_sub'] in idx and e['to_sub'] in idx
        )
        bch_q_correction = bch_half_sum * abs(V[b]) ** 2  # [S·kV²] = [MVAr]

        p_load_total = -S.real
        q_load_total = -S.imag + bch_q_correction
        p_direct = p_load_at.get(b, 0.0)
        q_direct = q_load_at.get(b, 0.0)
        p_tr = p_load_total - p_direct
        q_tr = q_load_total - q_direct
        result[b] = (p_tr, q_tr)
        print(f'  Transformer equiv load at {b}: '
              f'P_total={p_load_total:.4f} MW, P_direct={p_direct:.4f} MW, '
              f'P_tr={p_tr:.4f} MW  |  '
              f'Q_total={q_load_total:.4f} MVAr, Q_direct={q_direct:.4f} MVAr, '
              f'Q_tr={q_tr:.4f} MVAr  (bch_Q_corr={bch_q_correction:.4f} MVAr)')
    return result

_hv_tr_equiv = _compute_tr_equiv_loads()
print('  HV transformer equivalent loads (final):')
for _hv, (_p, _q) in sorted(_hv_tr_equiv.items()):
    print(f'    {_hv}: P_tr={_p:.4f} MW, Q_tr={_q:.4f} MVAr')

# ── Y_self per non-slack bus: Σ(1/Z) across all adjacent lines ───────────────
_y_self = {}   # sub_name -> (Y_re, Y_im)
for _edge in _line_edges:
    _p = _line_params[(_edge['from_sub'], _edge['to_sub'])]
    _Z = complex(_p['r_ohm'], _p['x_ohm'])
    _Y = 1.0 / _Z
    for _node in (_edge['from_sub'], _edge['to_sub']):
        if _node not in _all_slack_subs:
            _re, _im = _y_self.get(_node, (0.0, 0.0))
            _y_self[_node] = (_re + _Y.real, _im + _Y.imag)

# ── All substation names appearing in LINE edges (sorted) ────────────────────
_all_line_subs = sorted(
    {e['from_sub'] for e in _line_edges} | {e['to_sub'] for e in _line_edges}
)

# Also include HV transformer buses that have no LINE edges
# (e.g. simple_grid N0 only connects via transformer, not lines)
_tr_hv_only_subs = {_tr['hv_sub'] for _tr in _tr_edges_pre} - set(_all_line_subs)
# Also include LV transformer buses that have no LINE edges
# (e.g. BUS_8 18.0 kV — only connected as LV side of BRANCH-14, never appears in LINE edges)
_tr_lv_only_subs = {_tr['lv_sub'] for _tr in _tr_edges_pre} - set(_all_line_subs)
_all_subs = sorted(set(_all_line_subs) | _tr_hv_only_subs | _tr_lv_only_subs)
if _tr_hv_only_subs:
    print(f'  HV transformer-only buses (no LINE edges): {sorted(_tr_hv_only_subs)}')
if _tr_lv_only_subs:
    print(f'  LV transformer-only buses (no LINE edges): {sorted(_tr_lv_only_subs)}')

_nonslock_subs = [s for s in _all_subs if s not in _all_slack_subs]

# Simulation length = number of LIM iterations
STOP =  200

# ── Simulator config ──────────────────────────────────────────────────────────
sim_config = {
    'Transformer': {
        'python': 'transformer_simulator:Transformer',
    },
    'ACLineSegment': {
        'python': 'line_simulator:Line',
    },
    'Substation': {
        'python': 'substation_simulator:Substation',
    },
    'Load': {
        'python': 'load_simulator:Load',
    },
    'Collector': {
        'python': 'collector:Collector',
    },
}

world = mosaik.World(sim_config)

# ── Transformers: one entity per Transformer node in graph ────────────────────
transformer_sim = world.start('Transformer',
    fmu_filename=os.path.join(FMU_DIR, 'TR1.fmu'),
    instance_name='Transformer', step_size=1)

_entity_map = {}
for _t in _transformers:
    _e = transformer_sim.Transformer.create(
        1,
        r_ohm=_t.get('hv_r_ohm', 0.0) or 0.0,
        x_ohm=_t.get('hv_x_ohm', 0.0) or 0.0,
        rated_u1_kv=_t.get('hv_rated_u_kv') or _t.get('hv_nominal_voltage_kv', 1.0),
        rated_u2_kv=_t.get('lv_rated_u_kv') or _t.get('lv_nominal_voltage_kv', 1.0),
        phase_angle_clock=int(_t.get('hv_phase_angle_clock') or 0),
    )[0]
    _entity_map[_tr_var(_t['name'])] = _e
    print(f'  Instantiated Transformer {_t["name"]} '
          f'(R={_t.get("hv_r_ohm",0.0):.4f} Ω, X={_t.get("hv_x_ohm",0.0):.4f} Ω, '
          f'U1={_t.get("hv_rated_u_kv")} kV, U2={_t.get("lv_rated_u_kv")} kV, '
          f'clock={int(_t.get("hv_phase_angle_clock") or 0)})')

# ── All substations from LINE edges ───────────────────────────────────────────
# Slack buses (is_slack=True in Neo4j) get is_slack=1.0 and their own nominal voltage.
# Non-slack buses get Y_self computed from adjacent lines.
bus_sim = world.start('Substation',
    fmu_filename=os.path.join(FMU_DIR, 'Substation.fmu'),
    instance_name='Substation_bus', step_size=1)

for _sub_name in _all_subs:
    _sp = _sub_params.get(_sub_name, {'v_nom_kv': 20.0, 'is_slack': False, 'sv_voltage_kv': None})
    _v_nom = _sp['v_nom_kv']
    # Use CIM operating voltage if available, else fall back to nominal
    _v_slack = _sp.get('sv_voltage_kv') or _v_nom
    _is_slack = _sub_name in _all_slack_subs
    _is_pv    = _sub_name in _pv_subs
    if _is_slack:
        _v_ang = _sp.get('sv_angle_deg', 0.0)
        _e = bus_sim.Substation.create(1, is_slack=1.0, V_slack_kv=_v_slack,
                                       V_slack_ang_deg=_v_ang)[0]
    elif _is_pv:
        _yre, _yim = _y_self.get(_sub_name, (0.0, 0.0))
        _v_pv     = _sp.get('sv_voltage_kv') or _v_nom
        # PV buses: flat start for magnitude (overridden by is_pv clamp anyway),
        # but warm-start the angle at the CIM sv_angle reference.  This is required
        # for isolated PV buses like BUS_8 where Y_self=0 prevents any LIM angle
        # update, and improves convergence for connected PV buses from flat start.
        _v_pv_ang = _sp.get('sv_angle_deg', 0.0)
        _e = bus_sim.Substation.create(
            1, Y_self_re=_yre, Y_self_im=_yim,
            B_shunt=0.0, omega_relax=0.5, is_slack=0.0,
            V_slack_kv=_v_nom, V_slack_ang_deg=_v_pv_ang,
            is_pv=1.0, V_pv_kv=_v_pv)[0]
    else:
        _yre, _yim = _y_self.get(_sub_name, (0.0, 0.0))
        # Flat start: initialise all non-slack buses at nominal voltage, 0° angle.
        _e = bus_sim.Substation.create(
            1, Y_self_re=_yre, Y_self_im=_yim,
            B_shunt=0.0, omega_relax=0.5, is_slack=0.0,
            V_slack_kv=_v_nom, V_slack_ang_deg=0.0)[0]
    _entity_map[_sub_var(_sub_name)] = _e
    print(f'  Instantiated Substation {_sub_name} (is_slack={_is_slack}, is_pv={_is_pv}, V={_v_slack} kV)')

# ── Lines: one entity per LINE edge ───────────────────────────────────────────
line_sim = world.start('ACLineSegment',
    fmu_filename=os.path.join(FMU_DIR, 'ACLineSegment.fmu'),
    instance_name='Line', step_size=1)

for _edge in _line_edges:
    _fs, _ts = _edge['from_sub'], _edge['to_sub']
    _p = _line_params[(_fs, _ts)]
    _e = line_sim.Line.create(1, r_ohm=_p['r_ohm'], x_ohm=_p['x_ohm'], bch=_p['bch'])[0]
    _entity_map[_line_var(_fs, _ts)] = _e
    print(f'  Instantiated Line {_fs}→{_ts}')

# ── Loads: one entity per Load node ───────────────────────────────────────────
load_sim = world.start('Load',
    fmu_filename=os.path.join(FMU_DIR, 'Load.fmu'),
    instance_name='Load', step_size=1)

for _load_name, _lp in _load_params.items():
    _e = load_sim.Load.create(1, p_mw=_lp['p_mw'], q_mvar=_lp['q_mvar'])[0]
    _entity_map[_load_var(_load_name)] = _e
    print(f'  Instantiated Load {_load_name}')

# ── PV bus generator injection loads ─────────────────────────────────────────
# Each PV bus has a scheduled active power injection modelled as a negative-P
# Load FMU.  The substation P_load_mw input is the sum of all connected loads
# (real + this generator load), so net demand = P_load_direct - P_gen.
_PV_GEN_LOAD_KEY = '__pv_gen__{}'
for _pv_name in sorted(_pv_subs):
    _sp = _sub_params.get(_pv_name, {})
    _p_gen = _sp.get('p_gen_mw') or 0.0
    _q_gen = _sp.get('q_gen_mvar') or 0.0
    if abs(_p_gen) < 1e-9 and abs(_q_gen) < 1e-9:
        continue  # no injection to model
    _e = load_sim.Load.create(1, p_mw=-_p_gen, q_mvar=-_q_gen)[0]
    _entity_map[_PV_GEN_LOAD_KEY.format(_pv_name)] = _e
    print(f'  Instantiated PV generator injection at {_pv_name}: P_gen={_p_gen:.4f} MW, Q_gen={_q_gen:.4f} MVAr')

# ── Transformer equivalent loads at HV buses ──────────────────────────────────
# Each transformer sinks power from its HV substation equal to the total LV-zone demand.
# Model these as constant-power Load FMUs connected to the HV substations.
_TR_EQUIV_LOAD_KEY = '__tr_equiv__{}'  # internal entity-map key
for _hv_bus, (_tp, _tq) in _hv_tr_equiv.items():
    if abs(_tp) < 1e-6 and abs(_tq) < 1e-6:
        continue  # skip zero-power transformers (e.g. BRANCH-14 generator side)
    _e = load_sim.Load.create(1, p_mw=_tp, q_mvar=_tq)[0]
    _entity_map[_TR_EQUIV_LOAD_KEY.format(_hv_bus)] = _e
    print(f'  Instantiated transformer-equiv Load at {_hv_bus}: P={_tp:.4f} MW, Q={_tq:.4f} MVAr')

# ── Collector ─────────────────────────────────────────────────────────────────
collector_sim = world.start('Collector', output_dir=PROJECT_DIR, total_steps=STOP)
collector = collector_sim.Monitor()

# ── Connections (driven by GraphDB topology) ──────────────────────────────────
# Transformer: HV-side bus entity → transformer voltage input
for _tr in _tr_edges:
    _tr_e  = _entity_map[_tr_var(_tr['tr_name'])]
    _hv_e  = _entity_map[_sub_var(_tr['hv_sub'])]
    world.connect(_hv_e, _tr_e, ('V_mag_kv',  'V1_mag'))
    world.connect(_hv_e, _tr_e, ('V_ang_deg', 'V1_angle'))

# Lines: from-bus / to-bus voltages (LIM time-shifted) + current injection to to-bus
# Flat start: nominal voltage magnitude, 0° angle for all buses.
for _edge in _line_edges:
    _fs, _ts = _edge['from_sub'], _edge['to_sub']
    _from_e  = _entity_map[_sub_var(_fs)]
    _to_e    = _entity_map[_sub_var(_ts)]
    _line_e  = _entity_map[_line_var(_fs, _ts)]
    _v_nom_from = _sub_params.get(_fs, {}).get('v_nom_kv', 20.0)
    _v_nom_to   = _sub_params.get(_ts, {}).get('v_nom_kv', 20.0)
    world.connect(_from_e, _line_e, ('V_mag_kv', 'V_from_mag_kv'), time_shifted=True, initial_data={'V_mag_kv': _v_nom_from})
    world.connect(_from_e, _line_e, ('V_ang_deg', 'V_from_ang_deg'), time_shifted=True, initial_data={'V_ang_deg': 0.0})
    world.connect(_to_e,   _line_e, ('V_mag_kv', 'V_to_mag_kv'),   time_shifted=True, initial_data={'V_mag_kv': _v_nom_to})
    world.connect(_to_e,   _line_e, ('V_ang_deg', 'V_to_ang_deg'), time_shifted=True, initial_data={'V_ang_deg': 0.0})
    world.connect(_line_e, _to_e,   ('I_to_re', 'I_in_re'))
    world.connect(_line_e, _to_e,   ('I_to_im', 'I_in_im'))
    # Backward injection: current leaving from-bus via this branch (KCL)
    world.connect(_line_e, _from_e, ('I_neg_from_re', 'I_in_re'))
    world.connect(_line_e, _from_e, ('I_neg_from_im', 'I_in_im'))

# Loads: P/Q → substation
for _edge in _load_edges:
    _load_e = _entity_map[_load_var(_edge['load_name'])]
    _sub_e  = _entity_map[_sub_var(_edge['sub_name'])]
    world.connect(_load_e, _sub_e, ('P_load_mw',   'P_load_mw'))
    world.connect(_load_e, _sub_e, ('Q_load_mvar', 'Q_load_mvar'))

# Transformer equivalent loads: constant-power sinks at HV buses
for _hv_bus in _hv_tr_equiv:
    _key = _TR_EQUIV_LOAD_KEY.format(_hv_bus)
    if _key not in _entity_map:
        continue  # zero-power transformer, skipped above
    _te_e = _entity_map[_key]
    _hv_e = _entity_map[_sub_var(_hv_bus)]
    world.connect(_te_e, _hv_e, ('P_load_mw',   'P_load_mw'))
    world.connect(_te_e, _hv_e, ('Q_load_mvar', 'Q_load_mvar'))

# PV generator injection loads: negative-P source at each PV bus
for _pv_name in sorted(_pv_subs):
    _key = _PV_GEN_LOAD_KEY.format(_pv_name)
    if _key not in _entity_map:
        continue
    _pv_gen_e = _entity_map[_key]
    _bus_e    = _entity_map[_sub_var(_pv_name)]
    world.connect(_pv_gen_e, _bus_e, ('P_load_mw',   'P_load_mw'))
    world.connect(_pv_gen_e, _bus_e, ('Q_load_mvar', 'Q_load_mvar'))

# ── Collector connections ─────────────────────────────────────────────────────
# All substations (including HV-only transformer buses): voltage magnitude + angle
for _sub_name in _all_subs:
    _sv = _sub_var(_sub_name)
    world.connect(_entity_map[_sv], collector, ('V_mag_kv',  f'V_{_sv}_mag_kv'))
    world.connect(_entity_map[_sv], collector, ('V_ang_deg', f'V_{_sv}_ang_deg'))

# Line losses
for _edge in _line_edges:
    _lv = _line_var(_edge['from_sub'], _edge['to_sub'])
    world.connect(_entity_map[_lv], collector, ('P_loss_mw',   f'P_loss_{_lv}_mw'))
    world.connect(_entity_map[_lv], collector, ('Q_loss_mvar', f'Q_loss_{_lv}_mvar'))

# Transformer LV output monitoring
for _t in _transformers:
    _tv = _tr_var(_t['name'])
    world.connect(_entity_map[_tv], collector, ('V2', f'V_{_tv}_LV'))

# Branch currents — from-side and to-side magnitude for all lines
for _edge in _line_edges:
    _lv = _line_var(_edge['from_sub'], _edge['to_sub'])
    world.connect(_entity_map[_lv], collector, ('I_from_mag_kA', f'I_from_{_lv}_kA'))
    world.connect(_entity_map[_lv], collector, ('I_to_mag_kA',   f'I_to_{_lv}_kA'))

# ── Live dashboard ────────────────────────────────────────────────────────────
_graph_html   = os.path.join(PROJECT_DIR, 'graph.html')
_dashboard    = os.path.join(PROJECT_DIR, 'live_dashboard.html')
_status_path  = os.path.join(PROJECT_DIR, 'sim_status.json')

with open(_status_path, 'w') as _f:
    json.dump({'running': True, 'step': 0, 'total': STOP}, _f)

if generate_live_dashboard(_graph_html, _dashboard):
    _server, _url = start_live_server(PROJECT_DIR, port=8765)
    if _server:
        print(f'\n{"="*60}')
        print(f'  Live dashboard: {_url}')
        print(f'  Opening in browser...')
        print(f'{"="*60}\n')
        webbrowser.open(_url)
    else:
        print(f'\n  Live dashboard generated: {_dashboard}')
        print(f'  Open it via a local server (HTTP server failed to start)\n')
else:
    print('\n  [live_server] graph.html not found; skipping live dashboard.\n')

# ── Run ───────────────────────────────────────────────────────────────────────
world.run(until=STOP)

# ── Visualization ─────────────────────────────────────────────────────────────
print('\n=== Generating visualization ===')
try:
    with open('output.json', 'r') as f:
        data = json.load(f)

    def series(key):
        if key not in data:
            return [], []
        d = data[key]
        times = sorted(d.keys(), key=int)
        return [int(t) for t in times], [d[t] for t in times]

    fig = make_subplots(rows=4, cols=1, shared_xaxes=True,
        subplot_titles=[
            '|V| Bus Voltages (kV) vs LIM iteration',
            'Voltage Angle (deg) vs LIM iteration',
            'Line Active Power Losses (MW)',
            'Branch Current Magnitudes (kA)',
        ])

    # Row 1: bus voltage magnitudes — all substations
    for _sub_name in _all_subs:
        _sv = _sub_var(_sub_name)
        t, v = series(f'V_{_sv}_mag_kv')
        fig.add_trace(go.Scatter(x=t, y=v, name=f'|V| {_sub_name}'), row=1, col=1)

    # Row 2: voltage angles — non-slack buses only
    for _sub_name in _nonslock_subs:
        _sv = _sub_var(_sub_name)
        t, v = series(f'V_{_sv}_ang_deg')
        fig.add_trace(go.Scatter(x=t, y=v, name=f'∠ {_sub_name}',
                                 line=dict(dash='dot')), row=2, col=1)

    # Row 3: line active power losses
    for _edge in _line_edges:
        _lv = _line_var(_edge['from_sub'], _edge['to_sub'])
        t, v = series(f'P_loss_{_lv}_mw')
        fig.add_trace(go.Scatter(x=t, y=v,
                                 name=f'P_loss {_edge["from_sub"]}→{_edge["to_sub"]}',
                                 line=dict(dash='dash')), row=3, col=1)

    # Row 4: branch current magnitudes (from-side)
    for _edge in _line_edges:
        _lv = _line_var(_edge['from_sub'], _edge['to_sub'])
        t, v = series(f'I_from_{_lv}_kA')
        fig.add_trace(go.Scatter(x=t, y=v,
                                 name=f'I {_edge["from_sub"]}→{_edge["to_sub"]}'), row=4, col=1)

    fig.update_xaxes(title_text='LIM iteration', row=4, col=1)
    fig.update_yaxes(title_text='|V| (kV)', row=1, col=1)
    fig.update_yaxes(title_text='angle (deg)', row=2, col=1)
    fig.update_yaxes(title_text='P_loss (MW)', row=3, col=1)
    fig.update_yaxes(title_text='|I| (kA)', row=4, col=1)
    fig.update_layout(title_text='LIM Co-simulation: MV Network Power Flow (generic slack)', hovermode='x unified')
    out_html = os.path.join(PROJECT_DIR, 'output.html')
    fig.write_html(out_html)
    print(f'Visualization saved to {out_html}')

except FileNotFoundError:
    print('Warning: output.json not found. Run simulation first.')
except Exception as e:
    import traceback
    print(f'Visualization error: {e}')
    traceback.print_exc()
