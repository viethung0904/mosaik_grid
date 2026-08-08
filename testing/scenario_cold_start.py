"""
Mosaik co-simulation scenario — Cold-start convergence diagnostic (Adaptive NR).

Fork of scenario_adaptive.py for one purpose only: verify that the NR/Gauss-Jacobi
solver converges to the correct operating point WITHOUT the CIM SvVoltage warm start.

Cold start:
    Every non-enforced bus/line/transformer voltage is seeded at nominal magnitude,
    0 deg angle — NOT at the CIM SvVoltage solution. Two values remain untouched,
    because they are enforced physics, not just an initial guess:
      - V_slack_kv / V_slack_ang_deg on the true slack bus (is_slack=1) — recomputed
        from these parameters every single step inside SubstationNR.fmu.
      - V_reg_kv on SynchronousMachine (PV) buses — the regulation target the NR
        pins |V| to after every step.
    Everything else (V_slack_kv/V_slack_ang_deg seed on PQ/PV buses, and the
    initial_data on all line/transformer time_shifted connections) only affects
    the FIRST value fed into the solver, so flattening it is safe to test with.

Known risk (see SubstationNR.fmu's PV-bus solve): the SynchronousMachine branch
picks between two analytical angle roots by proximity to the bus's own previous
angle. A cold start could in principle lock onto the wrong root. Check the buses
in _sync_machine_subs land on the CIM reference angle, not just "a stable" angle.

Run length: 100 raw outer Gauss-Jacobi/NR sweeps at a FIXED load (N_LIM=1, so each
mosaik tick is one physical tick — no 24h time-varying profile). This isolates the
convergence-from-cold-start question from load/PV time variation.

Network data sourced from Neo4j GraphDB (scheme currently loaded — load the
standard IEEE-14 CIM file with database/add_data_slack_detection.py first if you
want a PV/Battery-free baseline).
"""
import os
import sys
import json
import webbrowser

from dotenv import load_dotenv
from neo4j import GraphDatabase
import mosaik
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from live_server import start_live_server, generate_live_dashboard

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, os.path.join(SCRIPT_DIR, 'simulator'))
sys.path.insert(0, SCRIPT_DIR)
# database/ on path so visualize can be imported for graph auto-regen
sys.path.insert(0, os.path.join(PROJECT_DIR, 'database'))

FMU_DIR = os.path.join(PROJECT_DIR, 'fmus')

# ── GraphDB helpers ────────────────────────────────────────────────────────────

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

        tr_records   = session.run('MATCH (t:Transformer) RETURN t ORDER BY t.name').data()
        transformers = [dict(r['t']) for r in tr_records]
        if not transformers:
            raise RuntimeError('No Transformer nodes found in graph database')

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
        if not line_params:
            raise RuntimeError('No LINE edges found in graph database')

        load_records = session.run(
            'MATCH (l:Load) RETURN l.name AS name, l.p_mw AS p_mw, l.q_mvar AS q_mvar '
            'ORDER BY l.name'
        ).data()
        load_params = {r['name']: {'p_mw': r['p_mw'], 'q_mvar': r['q_mvar']} for r in load_records}
        if not load_params:
            raise RuntimeError('No Load nodes found in graph database')

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


def _seed_v(sp):
    """
    Cold-start seed (V_kv, angle_deg) for a bus/line — nominal magnitude, 0 deg.
    Only ever used as an initial-condition seed, never for the enforced targets
    (V_slack_kv on the true slack bus, V_reg_kv on sync-machine buses).
    """
    return sp.get('v_nom_kv', 20.0), 0.0


# ── Fetch network data ─────────────────────────────────────────────────────────
print('Fetching network parameters from GraphDB...')
_sub_params, _transformers, _line_params, _load_params = fetch_all_network_params()

_slack_subs        = {name for name, sp in _sub_params.items() if sp['is_slack']}
_sync_machine_subs = {name for name, sp in _sub_params.items() if sp['is_sync_machine']}

print('Fetching transformer edges ...')
_, _, _tr_edges_pre = fetch_edges()
# LV transformer buses are NOT forced as slack — the correct transformer model
# (standard physical π-model with HV-referred impedance) allows them to be free
# PQ nodes that naturally converge to the CIM operating point.
_lv_slack_subs  = set()
_all_slack_subs = _slack_subs | _lv_slack_subs
print(f'  True slack buses (is_slack=True): {sorted(_slack_subs)}')
print(f'  SynchronousMachine buses:         {sorted(_sync_machine_subs)}')
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

# 100 raw outer Gauss-Jacobi/NR sweeps at a FIXED load (N_LIM=1 => 1 tick = 1 sweep).
# No 24h time-varying schedule here — the point is to watch convergence-from-cold
# in isolation, not to reproduce the quasi-static profile.
N_LIM = 1
STOP  = 300

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
# Cold start: V_slack_kv/V_slack_ang_deg passed here are only an initial-condition
# seed for PQ/sync-machine buses (never re-read after init) — flattened to nominal.
# The true slack bus's V_slack_kv (re-read every step) and sync-machine V_reg_kv
# (the regulation target) are left at the real CIM values, since those are physics.
bus_sim = world.start('SubstationNR',
    fmu_filename=os.path.join(FMU_DIR, 'SubstationNR.fmu'),
    instance_name='Substation_bus', step_size=1)

for _sub_name in _all_subs:
    _sp = _sub_params.get(_sub_name, {'v_nom_kv': 20.0, 'is_slack': False, 'sv_voltage_kv': None})
    _is_slack = _sub_name in _all_slack_subs
    _is_sync  = _sub_name in _sync_machine_subs
    if _is_slack:
        _v_slack = _sp.get('sv_voltage_kv') or _sp['v_nom_kv']   # true enforced value
        _e = bus_sim.SubstationNR.create(1, is_slack=1.0, V_slack_kv=_v_slack,
                                          V_slack_ang_deg=_sp.get('sv_angle_deg', 0.0))[0]
    elif _is_sync:
        _yre, _yim = _y_self.get(_sub_name, (0.0, 0.0))
        _v_reg = _sp.get('sv_voltage_kv') or _sp['v_nom_kv']     # true regulation target
        _v_seed, _ang_seed = _seed_v(_sp)                        # cold-start seed only
        _e = bus_sim.SubstationNR.create(
            1, Y_self_re=_yre, Y_self_im=_yim, B_shunt=0.0, omega_relax=0.5,
            is_slack=0.0, V_slack_kv=_v_seed, V_slack_ang_deg=_ang_seed,
            is_sync_machine=1.0, V_reg_kv=_v_reg)[0]
        _v_slack = _v_seed
    else:
        _yre, _yim = _y_self.get(_sub_name, (0.0, 0.0))
        _v_seed, _ang_seed = _seed_v(_sp)                        # cold-start seed only
        _e = bus_sim.SubstationNR.create(
            1, Y_self_re=_yre, Y_self_im=_yim, B_shunt=0.0, omega_relax=0.5,
            is_slack=0.0, V_slack_kv=_v_seed, V_slack_ang_deg=_ang_seed)[0]
        _v_slack = _v_seed
    _entity_map[_sub_var(_sub_name)] = _e
    print(f'  Substation {_sub_name}: is_slack={_is_slack}, is_sync={_is_sync}, V_seed={_v_slack} kV')

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
    _battery_e = battery_sim.Battery.create(1, p_charge_mw=_BATTERY_P_CHARGE)[0]
    _entity_map['battery_1'] = _battery_e
    print(f'  Battery-1 at {_BATTERY_BUS} (p_charge={_BATTERY_P_CHARGE} MW)')

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
# Transformer branches: HV + LV bus voltages (time_shifted) → current injections.
# Cold start: initial_data seeded at nominal/0deg instead of the CIM solution —
# this only affects the very first current calculation (step 0); from step 1
# onward the line/transformer FMUs read the bus's own actual output.
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
    _v_init_hv, _ang_hv = _seed_v(_sub_params.get(_db_hv, {}))
    _v_init_lv, _ang_lv = _seed_v(_sub_params.get(_db_lv, {}))
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

# Lines: time-shifted voltage inputs + current injections (Gauss-Jacobi).
# Cold start: initial_data seeded at nominal/0deg instead of the CIM solution.
for _edge in _line_edges:
    _fs, _ts = _edge['from_sub'], _edge['to_sub']
    _from_e = _entity_map[_sub_var(_fs)]
    _to_e   = _entity_map[_sub_var(_ts)]
    _line_e = _entity_map[_line_var(_fs, _ts)]
    _vf, _af = _seed_v(_sub_params.get(_fs, {}))
    _vt, _at = _seed_v(_sub_params.get(_ts, {}))
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
                  ('P_load_mw', 'P_load_mw'))

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
                  ('SOC',       'Battery1_SOC'),
                  ('P_load_mw', 'Battery1_P_load_mw'),
                  ('V_volt',    'Battery1_V_volt'),
                  ('I_amp',     'Battery1_I_amp'))

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
print('Saved output.json (raw per-tick data — see output_cold_start.json below)')

import shutil
shutil.copy(os.path.join(PROJECT_DIR, 'output.json'),
            os.path.join(PROJECT_DIR, 'output_cold_start.json'))
print('Saved output_cold_start.json')

# ── Post-simulation visualization ─────────────────────────────────────────────
print('\n=== Generating visualization ===')
try:
    with open(os.path.join(PROJECT_DIR, 'output.json'), 'r') as f:
        data = json.load(f)

    def series(key):
        if key not in data:
            return [], []
        d = data[key]
        times = sorted(d.keys(), key=int)
        return [int(t) for t in times], [d[t] for t in times]

    # ── Build subplot list dynamically ────────────────────────────────────────
    _subplot_titles = [
        '|V| Bus Voltages (kV) — vs. outer sweep #',
        'Voltage Angle (deg) — vs. outer sweep #',
        'Line Active Power Losses (MW)',
        'Branch Current Magnitudes (kA)',
    ]
    if _HAS_BATTERY:
        _subplot_titles += ['Battery: Charge / Discharge Power (MW)']
    if _HAS_PV:
        _subplot_titles += ['PV: Active Power (MW)']

    _n_rows = len(_subplot_titles)
    fig = make_subplots(rows=_n_rows, cols=1, shared_xaxes=True,
                        subplot_titles=_subplot_titles)

    # Row 1: bus voltage magnitudes — every raw sweep, not downsampled
    for _sub_name in _all_subs:
        _sv = _sub_var(_sub_name)
        t, v = series(f'V_{_sv}_mag_kv')
        fig.add_trace(go.Scatter(x=t, y=v, name=f'|V| {_sub_name}'), row=1, col=1)

    # Row 2: voltage angles (non-slack) — the buses to watch for wrong-root lock-in
    for _sub_name in _nonslock_subs:
        _sv = _sub_var(_sub_name)
        t, v = series(f'V_{_sv}_ang_deg')
        dash = 'solid' if _sub_name in _sync_machine_subs else 'dot'
        fig.add_trace(go.Scatter(x=t, y=v, name=f'∠ {_sub_name}',
                                 line=dict(dash=dash)), row=2, col=1)

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
        t, v = series('Battery1_P_load_mw')
        if t:
            fig.add_trace(go.Scatter(x=t, y=v, name='Battery P (MW)',
                                     line=dict(color='blue')), row=_row, col=1)
        _row += 1

    if _HAS_PV:
        t, v = series('PV1_P_load_mw')
        if t:
            fig.add_trace(go.Scatter(x=t, y=[-p for p in v], name='PV P_gen (MW)',
                                     line=dict(color='#F39C12')), row=_row, col=1)

    fig.update_xaxes(title_text='Outer Gauss-Jacobi sweep #', row=_n_rows, col=1)
    fig.update_yaxes(title_text='|V| (kV)',    row=1, col=1)
    fig.update_yaxes(title_text='angle (deg)', row=2, col=1)
    fig.update_yaxes(title_text='P_loss (MW)', row=3, col=1)
    fig.update_yaxes(title_text='|I| (kA)',    row=4, col=1)

    _scheme_label = _scheme if '_scheme' in dir() else 'Cold Start'
    fig.update_layout(
        title_text=f'Cold-Start Convergence: {_scheme_label} — {STOP} raw NR/GJ sweeps',
        hovermode='x unified',
    )
    out_html = os.path.join(PROJECT_DIR, 'output_cold_start_NR.html')
    fig.write_html(out_html)
    print(f'Visualization saved to {out_html}')

except FileNotFoundError:
    print('Warning: output.json not found.')
except Exception as e:
    import traceback
    print(f'Visualization error: {e}')
    traceback.print_exc()
