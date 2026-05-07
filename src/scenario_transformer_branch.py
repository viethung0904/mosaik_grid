"""
Mosaik co-simulation scenario — LIM power flow with π-model transformer current injection.

Improvements over scenario_IEEE_14.py:
  - Transformers now inject currents into both HV and LV buses (π-model, off-nominal tap).
  - LV transformer buses are NO LONGER forced to be slack nodes.
  - The _hv_tr_equiv constant-power load hack is completely removed.
  - Y_self per bus includes contributions from adjacent transformer branches.
  - BRANCH-14: impedance is on the LV end in CIM — referred to HV side automatically.

LIM mapping:
    Each mosaik timestep = one Jacobi LIM iteration.
    time_shifted=True on Sub→Branch connections provides the LIM one-step delay.
    omega_relax=0.5 required for convergence (spectral radius ρ_eff ≈ 0.5).

Network data sourced from Neo4j GraphDB.
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
from connect import fetch_edges, _sub_var, _line_var, _load_var, _tr_var

FMU_DIR = os.path.join(PROJECT_DIR, 'fmus')


# ── GraphDB: fetch all network parameters ─────────────────────────────────────
def fetch_all_network_params():
    load_dotenv(os.path.join(PROJECT_DIR, '.env'))
    driver = GraphDatabase.driver(
        os.getenv('NEO4J_URI'),
        auth=(os.getenv('NEO4J_USERNAME'), os.getenv('NEO4J_PASSWORD')),
    )
    try:
        with driver.session(database=os.getenv('NEO4J_DATABASE')) as session:
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
                    'v_nom_kv':      r['v_nom']  if r['v_nom']  is not None else 20.0,
                    'is_slack':      bool(r['is_slack']) if r['is_slack'] is not None else False,
                    'is_pv':         bool(r['is_pv'])    if r['is_pv']    is not None else False,
                    'sv_voltage_kv': r['sv_v']   if r['sv_v']   is not None else None,
                    'sv_angle_deg':  r['sv_ang'] if r['sv_ang'] is not None else 0.0,
                    'p_gen_mw':      r['p_gen_mw']   if r['p_gen_mw']   is not None else 0.0,
                    'q_gen_mvar':    r['q_gen_mvar'] if r['q_gen_mvar'] is not None else 0.0,
                }
                for r in sub_records
            }
            if not sub_params:
                raise RuntimeError('No Substation nodes found in graph database')

            tr_records = session.run(
                'MATCH (t:Transformer) RETURN t ORDER BY t.name'
            ).data()
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
                (r['from_sub'], r['to_sub']): {
                    'r_ohm': r['r_ohm'], 'x_ohm': r['x_ohm'], 'bch': r['bch'],
                }
                for r in line_records
            }
            if not line_params:
                raise RuntimeError('No LINE edges found in graph database')

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


def _tr_x_hv(t):
    """Return series reactance referred to the HV side [Ω].

    CIM stores the impedance on whichever winding End it belongs to.
    For BRANCH-14 the impedance is on the LV End; all others have it on the HV End.
    If hv_x_ohm is essentially zero but lv_x_ohm is non-zero, refer lv to HV.
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


# ── Fetch data ─────────────────────────────────────────────────────────────────
print('Fetching network parameters from GraphDB...')
_sub_params, _transformers, _line_params, _load_params = fetch_all_network_params()

_slack_subs = {name for name, sp in _sub_params.items() if sp['is_slack']}
_pv_subs    = {name for name, sp in _sub_params.items() if sp['is_pv']}

print(f'  True slack buses (is_slack=True): {sorted(_slack_subs)}')
print(f'  PV generator buses (is_pv=True):  {sorted(_pv_subs)}')

print('Fetching topology edges from GraphDB...')
_line_edges, _load_edges, _tr_edges = fetch_edges()
_tr_edges_pre = _tr_edges
print(f'  {len(_tr_edges)} transformer, {len(_line_edges)} line, {len(_load_edges)} load edge(s)')

# ── LV slack bus detection ────────────────────────────────────────────────────
# Purely inductive transformers (r=0) create a purely imaginary Y_self at LV terminals.
# The Jacobi LIM iteration cannot converge the angle of a galvanically isolated LV zone
# through purely reactive coupling: the correct operating-point is an UNSTABLE fixed
# point of Jacobi, so the iteration always converges to a spurious angle.
# Fix: treat all LV transformer terminal buses as slack (voltage+angle from SV data).
# This anchors the LV zone angle reference while the TransformerBranch FMU still
# computes the correct current injections at HV buses for HV convergence.
_lv_slack_subs = {_tr['lv_sub'] for _tr in _tr_edges_pre if _tr['lv_sub'] not in _slack_subs}
_all_slack_subs = _slack_subs | _lv_slack_subs
print(f'  LV transformer buses (slack, angle from SV): {sorted(_lv_slack_subs)}')

for _t in _transformers:
    _r_hv, _x_hv = _tr_x_hv(_t)
    _u1 = _t.get('hv_rated_u_kv') or _t.get('hv_nominal_voltage_kv')
    _u2 = _t.get('lv_rated_u_kv') or _t.get('lv_nominal_voltage_kv')
    print(f"  Transformer {_t['name']}: U1={_u1} kV / U2={_u2} kV  "
          f"R_hv={_r_hv:.6f} Ω  X_hv={_x_hv:.6f} Ω")
for (_fs, _ts), _lp in _line_params.items():
    print(f"  Line {_fs}→{_ts}: r={_lp['r_ohm']} Ω  x={_lp['x_ohm']} Ω  bch={_lp['bch']} S")
for _ln, _lp in _load_params.items():
    print(f"  {_ln}: p={_lp['p_mw']} MW  q={_lp['q_mvar']} MVAr")


# ── Y_self per bus: lines + transformer branches ───────────────────────────────
# For each bus:  Y_self = Σ(y_line) + Σ(y_tr_contribution)
# Line:          y = 1/(r+jx)         → +y to both endpoints
# Transformer:   y_s = 1/(r_hv+jx_hv)
#                → +y_s/t² to HV bus, +y_s to LV bus
_y_self = {}   # sub_name -> (Y_re, Y_im)

def _add_y(bus, y):
    if bus in _all_slack_subs:
        return
    re, im = _y_self.get(bus, (0.0, 0.0))
    _y_self[bus] = (re + y.real, im + y.imag)

# Contributions from line branches
for _edge in _line_edges:
    _p = _line_params[(_edge['from_sub'], _edge['to_sub'])]
    _Z = complex(_p['r_ohm'], _p['x_ohm'])
    _Y = 1.0 / _Z
    _add_y(_edge['from_sub'], _Y)
    _add_y(_edge['to_sub'],   _Y)

# Contributions from transformer branches
for _t in _transformers:
    _r_hv, _x_hv = _tr_x_hv(_t)
    _Z_tr = complex(_r_hv, _x_hv)
    if abs(_Z_tr) < 1e-15:
        continue
    _y_s = 1.0 / _Z_tr
    _u1  = _t.get('hv_rated_u_kv') or _t.get('hv_nominal_voltage_kv') or 1.0
    _u2  = _t.get('lv_rated_u_kv') or _t.get('lv_nominal_voltage_kv') or 1.0
    _t_ratio = _u1 / _u2

    # Find HV and LV bus names for this transformer from _tr_edges
    _hv_bus = next((_tr['hv_sub'] for _tr in _tr_edges_pre if _tr['tr_name'] == _t['name']), None)
    _lv_bus = next((_tr['lv_sub'] for _tr in _tr_edges_pre if _tr['tr_name'] == _t['name']), None)

    # Fix: for generator step-up transformers (e.g. BRANCH-14), the Neo4j graph can
    # label the lower-voltage bus as "HV" (because its winding has the higher rated_u).
    # Detect this via nominal bus voltages and swap to get correct Y_self contributions:
    # the bus connected to the winding with rated_u = _u1 should get y_s/t².
    _hv_bus_nom = _sub_params.get(_hv_bus, {}).get('v_nom_kv', 0.0) if _hv_bus else 0.0
    _lv_bus_nom = _sub_params.get(_lv_bus, {}).get('v_nom_kv', 0.0) if _lv_bus else 0.0
    if _hv_bus and _lv_bus and _hv_bus_nom < _lv_bus_nom:
        _hv_bus, _lv_bus = _lv_bus, _hv_bus
        print(f"  [Y_self] Swapped HV/LV for {_t['name']}: "
              f"hv_bus={_hv_bus} ({_hv_bus_nom}->{_lv_bus_nom} kV), lv_bus={_lv_bus}")

    if _hv_bus:
        _add_y(_hv_bus, _y_s / _t_ratio**2)
    if _lv_bus:
        _add_y(_lv_bus, _y_s)


# ── All substation names ──────────────────────────────────────────────────────
_all_line_subs  = sorted(
    {e['from_sub'] for e in _line_edges} | {e['to_sub'] for e in _line_edges}
)
_tr_hv_only_subs = {_tr['hv_sub'] for _tr in _tr_edges_pre} - set(_all_line_subs)
_tr_lv_only_subs = {_tr['lv_sub'] for _tr in _tr_edges_pre} - set(_all_line_subs)
_all_subs = sorted(set(_all_line_subs) | _tr_hv_only_subs | _tr_lv_only_subs)

if _tr_hv_only_subs:
    print(f'  HV transformer-only buses: {sorted(_tr_hv_only_subs)}')
if _tr_lv_only_subs:
    print(f'  LV transformer-only buses: {sorted(_tr_lv_only_subs)}')

_nonslack_subs = [s for s in _all_subs if s not in _all_slack_subs]

print('Y_self per non-slack bus:')
for _bus in sorted(_nonslack_subs):
    _yre, _yim = _y_self.get(_bus, (0.0, 0.0))
    print(f'  {_bus}: Y_self = ({_yre:.6f}, {_yim:.6f}) S')

STOP = 500

# ── Simulator config ──────────────────────────────────────────────────────────
sim_config = {
    'TransformerBranch': {
        'python': 'transformer_branch_simulator:TransformerBranch',
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

# ── Transformer branch entities ───────────────────────────────────────────────
tr_branch_sim = world.start('TransformerBranch',
    fmu_filename=os.path.join(FMU_DIR, 'TransformerBranch.fmu'),
    instance_name='TrBranch', step_size=1)

_entity_map = {}
for _t in _transformers:
    _r_hv, _x_hv = _tr_x_hv(_t)
    _u1 = _t.get('hv_rated_u_kv') or _t.get('hv_nominal_voltage_kv') or 1.0
    _u2 = _t.get('lv_rated_u_kv') or _t.get('lv_nominal_voltage_kv') or 1.0
    _e = tr_branch_sim.TransformerBranch.create(
        1,
        r_hv_ohm=_r_hv,
        x_hv_ohm=_x_hv,
        rated_u1_kv=_u1,
        rated_u2_kv=_u2,
    )[0]
    _entity_map[_tr_var(_t['name'])] = _e
    print(f"  Instantiated TransformerBranch {_t['name']}  "
          f"(R={_r_hv:.6f} Ω, X={_x_hv:.6f} Ω, U1={_u1} kV, U2={_u2} kV)")

# ── Substation entities ───────────────────────────────────────────────────────
bus_sim = world.start('Substation',
    fmu_filename=os.path.join(FMU_DIR, 'Substation.fmu'),
    instance_name='Substation_bus', step_size=1)

for _sub_name in _all_subs:
    _sp      = _sub_params.get(_sub_name, {'v_nom_kv': 20.0, 'is_slack': False, 'sv_voltage_kv': None})
    _v_nom   = _sp['v_nom_kv']
    _v_slack = _sp.get('sv_voltage_kv') or _v_nom
    _is_slack = _sub_name in _all_slack_subs
    _is_pv    = _sub_name in _pv_subs

    if _is_slack:
        _v_ang = _sp.get('sv_angle_deg', 0.0)
        _e = bus_sim.Substation.create(1, is_slack=1.0, V_slack_kv=_v_slack,
                                       V_slack_ang_deg=_v_ang)[0]
    elif _is_pv:
        _yre, _yim = _y_self.get(_sub_name, (0.0, 0.0))
        _v_pv      = _sp.get('sv_voltage_kv') or _v_nom
        _v_pv_ang  = _sp.get('sv_angle_deg', 0.0) or 0.0
        _v_init    = _sp.get('sv_voltage_kv') or (_v_nom / 5.0)
        _e = bus_sim.Substation.create(
            1, Y_self_re=_yre, Y_self_im=_yim,
            B_shunt=0.0, omega_relax=0.5, is_slack=0.0,
            V_slack_kv=_v_init, V_slack_ang_deg=_v_pv_ang,
            is_pv=1.0, V_pv_kv=_v_pv)[0]
    else:
        _yre, _yim = _y_self.get(_sub_name, (0.0, 0.0))
        _v_init    = _sp.get('sv_voltage_kv') or (_v_nom / 5.0)
        _v_ang_init = _sp.get('sv_angle_deg', 0.0) or 0.0
        _e = bus_sim.Substation.create(
            1, Y_self_re=_yre, Y_self_im=_yim,
            B_shunt=0.0, omega_relax=0.5, is_slack=0.0,
            V_slack_kv=_v_init, V_slack_ang_deg=_v_ang_init)[0]

    _entity_map[_sub_var(_sub_name)] = _e
    print(f'  Instantiated Substation {_sub_name} '
          f'(is_slack={_is_slack}, is_pv={_is_pv}, Y_self={_y_self.get(_sub_name, (0,0))})')

# ── Line entities ─────────────────────────────────────────────────────────────
line_sim = world.start('ACLineSegment',
    fmu_filename=os.path.join(FMU_DIR, 'ACLineSegment.fmu'),
    instance_name='Line', step_size=1)

for _edge in _line_edges:
    _fs, _ts = _edge['from_sub'], _edge['to_sub']
    _p = _line_params[(_fs, _ts)]
    _e = line_sim.Line.create(1, r_ohm=_p['r_ohm'], x_ohm=_p['x_ohm'], bch=_p['bch'])[0]
    _entity_map[_line_var(_fs, _ts)] = _e
    print(f'  Instantiated Line {_fs}→{_ts}')

# ── Load entities ─────────────────────────────────────────────────────────────
load_sim = world.start('Load',
    fmu_filename=os.path.join(FMU_DIR, 'Load.fmu'),
    instance_name='Load', step_size=1)

for _load_name, _lp in _load_params.items():
    _e = load_sim.Load.create(1, p_mw=_lp['p_mw'], q_mvar=_lp['q_mvar'])[0]
    _entity_map[_load_var(_load_name)] = _e
    print(f'  Instantiated Load {_load_name}')

# ── PV bus generator injection loads ─────────────────────────────────────────
_PV_GEN_LOAD_KEY = '__pv_gen__{}'
for _pv_name in sorted(_pv_subs):
    _sp    = _sub_params.get(_pv_name, {})
    _p_gen = _sp.get('p_gen_mw') or 0.0
    _q_gen = _sp.get('q_gen_mvar') or 0.0
    if abs(_p_gen) < 1e-9 and abs(_q_gen) < 1e-9:
        continue
    _e = load_sim.Load.create(1, p_mw=-_p_gen, q_mvar=-_q_gen)[0]
    _entity_map[_PV_GEN_LOAD_KEY.format(_pv_name)] = _e
    print(f'  Instantiated PV generator injection at {_pv_name}: '
          f'P_gen={_p_gen:.4f} MW, Q_gen={_q_gen:.4f} MVAr')

# ── Collector ─────────────────────────────────────────────────────────────────
collector_sim = world.start('Collector', output_dir=PROJECT_DIR, total_steps=STOP)
collector = collector_sim.Monitor()

# ── Connections ───────────────────────────────────────────────────────────────

# Transformer branches: both HV and LV voltages time-shifted → current injections
for _tr in _tr_edges:
    _tr_e   = _entity_map[_tr_var(_tr['tr_name'])]
    _db_hv  = _tr['hv_sub']   # bus labeled HV in DB (may be physically lower voltage)
    _db_lv  = _tr['lv_sub']   # bus labeled LV in DB (may be physically higher voltage)

    # Correct generator step-up transformers: swap if DB "HV" bus has lower nominal V
    _db_hv_nom = _sub_params.get(_db_hv, {}).get('v_nom_kv', 0.0)
    _db_lv_nom = _sub_params.get(_db_lv, {}).get('v_nom_kv', 0.0)
    if _db_hv_nom < _db_lv_nom:
        _db_hv, _db_lv = _db_lv, _db_hv
        print(f"  [Connect] Swapped HV/LV for {_tr['tr_name']}: V_hv→{_db_hv}, V_lv→{_db_lv}")

    _hv_e  = _entity_map[_sub_var(_db_hv)]
    _lv_e  = _entity_map[_sub_var(_db_lv)]
    _v_nom_hv  = _sub_params.get(_db_hv, {}).get('v_nom_kv', 69.0)
    _v_nom_lv  = _sub_params.get(_db_lv, {}).get('v_nom_kv', 13.8)
    _v_init_hv = _sub_params.get(_db_hv, {}).get('sv_voltage_kv') or _v_nom_hv
    _v_init_lv = _sub_params.get(_db_lv, {}).get('sv_voltage_kv') or _v_nom_lv
    _ang_hv    = _sub_params.get(_db_hv, {}).get('sv_angle_deg', 0.0) or 0.0
    _ang_lv    = _sub_params.get(_db_lv, {}).get('sv_angle_deg', 0.0) or 0.0

    world.connect(_hv_e, _tr_e, ('V_mag_kv', 'V_hv_mag_kv'),
                  time_shifted=True, initial_data={'V_mag_kv': _v_init_hv})
    world.connect(_hv_e, _tr_e, ('V_ang_deg', 'V_hv_ang_deg'),
                  time_shifted=True, initial_data={'V_ang_deg': _ang_hv})
    world.connect(_lv_e, _tr_e, ('V_mag_kv', 'V_lv_mag_kv'),
                  time_shifted=True, initial_data={'V_mag_kv': _v_init_lv})
    world.connect(_lv_e, _tr_e, ('V_ang_deg', 'V_lv_ang_deg'),
                  time_shifted=True, initial_data={'V_ang_deg': _ang_lv})

    # Current injections back into both buses
    world.connect(_tr_e, _hv_e, ('I_hv_in_re', 'I_in_re'))
    world.connect(_tr_e, _hv_e, ('I_hv_in_im', 'I_in_im'))
    world.connect(_tr_e, _lv_e, ('I_lv_in_re', 'I_in_re'))
    world.connect(_tr_e, _lv_e, ('I_lv_in_im', 'I_in_im'))

# Lines: time-shifted voltages → current injections (same as before)
for _edge in _line_edges:
    _fs, _ts = _edge['from_sub'], _edge['to_sub']
    _from_e  = _entity_map[_sub_var(_fs)]
    _to_e    = _entity_map[_sub_var(_ts)]
    _line_e  = _entity_map[_line_var(_fs, _ts)]
    _v_nom_from  = _sub_params.get(_fs, {}).get('v_nom_kv', 20.0)
    _v_nom_to    = _sub_params.get(_ts, {}).get('v_nom_kv', 20.0)
    _v_init_from = _sub_params.get(_fs, {}).get('sv_voltage_kv') or _v_nom_from
    _v_init_to   = _sub_params.get(_ts, {}).get('sv_voltage_kv') or _v_nom_to

    world.connect(_from_e, _line_e, ('V_mag_kv', 'V_from_mag_kv'),
                  time_shifted=True, initial_data={'V_mag_kv': _v_init_from})
    _ang_from = _sub_params.get(_fs, {}).get('sv_angle_deg', 0.0) or 0.0
    _ang_to   = _sub_params.get(_ts, {}).get('sv_angle_deg', 0.0) or 0.0
    world.connect(_from_e, _line_e, ('V_ang_deg', 'V_from_ang_deg'),
                  time_shifted=True, initial_data={'V_ang_deg': _ang_from})
    world.connect(_to_e,   _line_e, ('V_mag_kv', 'V_to_mag_kv'),
                  time_shifted=True, initial_data={'V_mag_kv': _v_init_to})
    world.connect(_to_e,   _line_e, ('V_ang_deg', 'V_to_ang_deg'),
                  time_shifted=True, initial_data={'V_ang_deg': _ang_to})
    world.connect(_line_e, _to_e,   ('I_to_re', 'I_in_re'))
    world.connect(_line_e, _to_e,   ('I_to_im', 'I_in_im'))
    world.connect(_line_e, _from_e, ('I_neg_from_re', 'I_in_re'))
    world.connect(_line_e, _from_e, ('I_neg_from_im', 'I_in_im'))

# Loads → substations
for _edge in _load_edges:
    _load_e = _entity_map[_load_var(_edge['load_name'])]
    _sub_e  = _entity_map[_sub_var(_edge['sub_name'])]
    world.connect(_load_e, _sub_e, ('P_load_mw',   'P_load_mw'))
    world.connect(_load_e, _sub_e, ('Q_load_mvar', 'Q_load_mvar'))

# PV generator injection loads
for _pv_name in sorted(_pv_subs):
    _key = _PV_GEN_LOAD_KEY.format(_pv_name)
    if _key not in _entity_map:
        continue
    world.connect(_entity_map[_key], _entity_map[_sub_var(_pv_name)],
                  ('P_load_mw', 'P_load_mw'), ('Q_load_mvar', 'Q_load_mvar'))

# ── Collector connections ─────────────────────────────────────────────────────
for _sub_name in _all_subs:
    _sv = _sub_var(_sub_name)
    world.connect(_entity_map[_sv], collector, ('V_mag_kv',  f'V_{_sv}_mag_kv'))
    world.connect(_entity_map[_sv], collector, ('V_ang_deg', f'V_{_sv}_ang_deg'))

for _edge in _line_edges:
    _lv = _line_var(_edge['from_sub'], _edge['to_sub'])
    world.connect(_entity_map[_lv], collector, ('P_loss_mw',   f'P_loss_{_lv}_mw'))
    world.connect(_entity_map[_lv], collector, ('Q_loss_mvar', f'Q_loss_{_lv}_mvar'))
    world.connect(_entity_map[_lv], collector, ('I_from_mag_kA', f'I_from_{_lv}_kA'))
    world.connect(_entity_map[_lv], collector, ('I_to_mag_kA',   f'I_to_{_lv}_kA'))

for _t in _transformers:
    _tv = _tr_var(_t['name'])
    world.connect(_entity_map[_tv], collector, ('P_loss_mw',   f'P_loss_{_tv}_mw'))
    world.connect(_entity_map[_tv], collector, ('Q_loss_mvar', f'Q_loss_{_tv}_mvar'))

# ── Live dashboard ────────────────────────────────────────────────────────────
_graph_html  = os.path.join(PROJECT_DIR, 'graph.html')
_dashboard   = os.path.join(PROJECT_DIR, 'live_dashboard.html')
_status_path = os.path.join(PROJECT_DIR, 'sim_status.json')

with open(_status_path, 'w') as _f:
    json.dump({'running': True, 'step': 0, 'total': STOP}, _f)

if generate_live_dashboard(_graph_html, _dashboard):
    _server, _url = start_live_server(PROJECT_DIR, port=8765)
    if _server:
        print(f'\n{"="*60}')
        print(f'  Live dashboard: {_url}')
        print(f'{"="*60}\n')
        webbrowser.open(_url)
    else:
        print(f'\n  Live dashboard generated: {_dashboard}\n')
else:
    print('\n  [live_server] graph.html not found; skipping live dashboard.\n')

# ── Run ───────────────────────────────────────────────────────────────────────
world.run(until=STOP)

# ── Visualization ─────────────────────────────────────────────────────────────
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

    fig = make_subplots(rows=3, cols=1, shared_xaxes=True,
        subplot_titles=[
            '|V| Bus Voltages (kV) vs LIM iteration',
            'Voltage Angle (deg) vs LIM iteration',
            'Line Active Power Losses (MW)',
        ])

    for _sub_name in _all_subs:
        _sv = _sub_var(_sub_name)
        t, v = series(f'V_{_sv}_mag_kv')
        fig.add_trace(go.Scatter(x=t, y=v, name=f'|V| {_sub_name}'), row=1, col=1)

    for _sub_name in _nonslack_subs:
        _sv = _sub_var(_sub_name)
        t, v = series(f'V_{_sv}_ang_deg')
        fig.add_trace(go.Scatter(x=t, y=v, name=f'∠ {_sub_name}',
                                 line=dict(dash='dot')), row=2, col=1)

    for _edge in _line_edges:
        _lv = _line_var(_edge['from_sub'], _edge['to_sub'])
        t, v = series(f'P_loss_{_lv}_mw')
        fig.add_trace(go.Scatter(x=t, y=v,
                                 name=f'{_edge["from_sub"]}→{_edge["to_sub"]}',
                                 line=dict(dash='dash')), row=3, col=1)

    fig.update_xaxes(title_text='LIM iteration', row=3, col=1)
    fig.update_yaxes(title_text='|V| (kV)',   row=1, col=1)
    fig.update_yaxes(title_text='angle (deg)', row=2, col=1)
    fig.update_yaxes(title_text='P_loss (MW)', row=3, col=1)
    fig.update_layout(
        title_text='LIM Co-simulation: Transformer π-model (no forced LV slack)',
        hovermode='x unified',
    )
    out_html = os.path.join(PROJECT_DIR, 'output.html')
    fig.write_html(out_html)
    print(f'Visualization saved to {out_html}')

except FileNotFoundError:
    print('Warning: output.json not found.')
except Exception as e:
    import traceback
    print(f'Visualization error: {e}')
    traceback.print_exc()
