"""
Mosaik co-simulation scenario — LIM-based MV network power flow.
Graph-driven: topology, parameters, and FMU instantiation counts all derived from Neo4j.

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
    Query Neo4j generically for all transformers, lines, and loads.

    Returns:
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

    return transformers, line_params, load_params


print('Fetching network parameters from GraphDB...')
_transformers, _line_params, _load_params = fetch_all_network_params()
for _t in _transformers:
    print(f"  Transformer {_t['name']}: hv={_t['hv_nominal_voltage_kv']} kV / lv={_t['lv_nominal_voltage_kv']} kV")
for (_fs, _ts), _lp in _line_params.items():
    print(f"  Line {_fs}→{_ts}: r={_lp['r_ohm']} Ω  x={_lp['x_ohm']} Ω  bch={_lp['bch']} S")
for _ln, _lp in _load_params.items():
    print(f"  {_ln}: p={_lp['p_mw']} MW  q={_lp['q_mvar']} MVAr")

print('Fetching topology edges from GraphDB...')
_line_edges, _load_edges, _tr_edges = fetch_edges()
print(f'  {len(_tr_edges)} transformer, {len(_line_edges)} line, {len(_load_edges)} load edge(s)')

# ── MV nominal voltage [kV] — LV side of first transformer ───────────────────
V_MV_KV = _transformers[0]['lv_nominal_voltage_kv']

# ── Slack buses = LV side of each transformer ─────────────────────────────────
_slack_subs = {tr['lv_sub'] for tr in _tr_edges}

# ── Y_self per non-slack bus: Σ(1/Z) across all adjacent lines ───────────────
_y_self = {}   # sub_name -> (Y_re, Y_im)
for _edge in _line_edges:
    _p = _line_params[(_edge['from_sub'], _edge['to_sub'])]
    _Z = complex(_p['r_ohm'], _p['x_ohm'])
    _Y = 1.0 / _Z
    for _node in (_edge['from_sub'], _edge['to_sub']):
        if _node not in _slack_subs:
            _re, _im = _y_self.get(_node, (0.0, 0.0))
            _y_self[_node] = (_re + _Y.real, _im + _Y.imag)

# ── All substation names appearing in LINE edges (sorted) ────────────────────
_all_line_subs = sorted(
    {e['from_sub'] for e in _line_edges} | {e['to_sub'] for e in _line_edges}
)
_nonslock_subs = [s for s in _all_line_subs if s not in _slack_subs]

# Simulation length = number of LIM iterations
STOP = 50

# ── Simulator config ──────────────────────────────────────────────────────────
sim_config = {
    'V_source': {
        'python': 'source_simulator:V_source',
    },
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

# ── V_source (single HV slack) ────────────────────────────────────────────────
v_source_sim = world.start('V_source',
    fmu_filename=os.path.join(FMU_DIR, 'V_source.fmu'),
    instance_name='V_source', step_size=1)
v_source = v_source_sim.V_source.create(1)[0]

# ── Transformers: one entity per Transformer node in graph ────────────────────
transformer_sim = world.start('Transformer',
    fmu_filename=os.path.join(FMU_DIR, 'TR1.fmu'),
    instance_name='Transformer', step_size=1)

_entity_map = {'v_source': v_source}
for _t in _transformers:
    _e = transformer_sim.Transformer.create(1)[0]
    _entity_map[_tr_var(_t['name'])] = _e
    print(f'  Instantiated Transformer {_t["name"]}')

# ── MV slack monitoring buses: one per transformer LV bus ─────────────────────
# Kept in a separate simulator instance to avoid algebraic loops with load buses
slack_sim = world.start('Substation',
    fmu_filename=os.path.join(FMU_DIR, 'Substation.fmu'),
    instance_name='Substation_slack', step_size=1)

_slack_monitor_map = {}   # lv_sub_name -> monitoring entity (collector only)
for _tr in _tr_edges:
    _lv = _tr['lv_sub']
    _e = slack_sim.Substation.create(1, is_slack=1.0, V_slack_kv=V_MV_KV)[0]
    _slack_monitor_map[_lv] = _e
    print(f'  Instantiated slack monitor for LV bus {_lv}')

# ── All substations from LINE edges ───────────────────────────────────────────
# Slack buses (LV side of a transformer) get is_slack=1.0; others get Y_self.
bus_sim = world.start('Substation',
    fmu_filename=os.path.join(FMU_DIR, 'Substation.fmu'),
    instance_name='Substation_bus', step_size=1)

for _sub_name in _all_line_subs:
    if _sub_name in _slack_subs:
        _e = bus_sim.Substation.create(1, is_slack=1.0, V_slack_kv=V_MV_KV)[0]
    else:
        _yre, _yim = _y_self.get(_sub_name, (0.0, 0.0))
        _e = bus_sim.Substation.create(
            1, Y_self_re=_yre, Y_self_im=_yim,
            B_shunt=0.0, omega_relax=0.5, is_slack=0.0, V_slack_kv=V_MV_KV)[0]
    _entity_map[_sub_var(_sub_name)] = _e
    print(f'  Instantiated Substation {_sub_name} (slack={_sub_name in _slack_subs})')

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

# ── Collector ─────────────────────────────────────────────────────────────────
collector_sim = world.start('Collector', output_dir=PROJECT_DIR, total_steps=STOP)
collector = collector_sim.Monitor()

# ── Connections (driven by GraphDB topology) ──────────────────────────────────
# Transformer: v_source → transformer voltage signals
for _tr in _tr_edges:
    _tr_e = _entity_map[_tr_var(_tr['tr_name'])]
    world.connect(_entity_map['v_source'], _tr_e, ('V_source_mag',   'V1_mag'))
    world.connect(_entity_map['v_source'], _tr_e, ('V_source_angle', 'V1_angle'))

# Lines: from-bus / to-bus voltages (LIM time-shifted) + current injection to to-bus
for _edge in _line_edges:
    _fs, _ts = _edge['from_sub'], _edge['to_sub']
    _from_e  = _entity_map[_sub_var(_fs)]
    _to_e    = _entity_map[_sub_var(_ts)]
    _line_e  = _entity_map[_line_var(_fs, _ts)]
    world.connect(_from_e, _line_e, ('V_mag_kv', 'V_from_mag_kv'), time_shifted=True, initial_data={'V_mag_kv': V_MV_KV})
    world.connect(_from_e, _line_e, ('V_ang_deg', 'V_from_ang_deg'), time_shifted=True, initial_data={'V_ang_deg': 0.0})
    world.connect(_to_e,   _line_e, ('V_mag_kv', 'V_to_mag_kv'),   time_shifted=True, initial_data={'V_mag_kv': V_MV_KV})
    world.connect(_to_e,   _line_e, ('V_ang_deg', 'V_to_ang_deg'), time_shifted=True, initial_data={'V_ang_deg': 0.0})
    world.connect(_line_e, _to_e,   ('I_to_re', 'I_in_re'))
    world.connect(_line_e, _to_e,   ('I_to_im', 'I_in_im'))

# Loads: P/Q → substation
for _edge in _load_edges:
    _load_e = _entity_map[_load_var(_edge['load_name'])]
    _sub_e  = _entity_map[_sub_var(_edge['sub_name'])]
    world.connect(_load_e, _sub_e, ('P_load_mw',   'P_load_mw'))
    world.connect(_load_e, _sub_e, ('Q_load_mvar', 'Q_load_mvar'))

# ── Collector connections ─────────────────────────────────────────────────────
# MV slack monitoring buses (one per transformer LV bus)
for _lv, _slack_e in _slack_monitor_map.items():
    world.connect(_slack_e, collector, ('V_mag_kv', f'V_slack_{_sub_var(_lv)}_mag_kv'))

# All substations from LINE edges: voltage magnitude + angle
for _sub_name in _all_line_subs:
    _sv = _sub_var(_sub_name)
    world.connect(_entity_map[_sv], collector, ('V_mag_kv',  f'V_{_sv}_mag_kv'))
    world.connect(_entity_map[_sv], collector, ('V_ang_deg', f'V_{_sv}_ang_deg'))

# Line losses
for _edge in _line_edges:
    _lv = _line_var(_edge['from_sub'], _edge['to_sub'])
    world.connect(_entity_map[_lv], collector, ('P_loss_mw',   f'P_loss_{_lv}_mw'))
    world.connect(_entity_map[_lv], collector, ('Q_loss_mvar', f'Q_loss_{_lv}_mvar'))

# HV monitoring
world.connect(v_source, collector, ('V_source_mag', 'V_HV_mag'))
for _t in _transformers:
    _tv = _tr_var(_t['name'])
    world.connect(_entity_map[_tv], collector, ('V2', f'V_{_tv}_LV'))

# ── Live dashboard ────────────────────────────────────────────────────────────
_graph_html   = os.path.join(PROJECT_DIR, 'graph.html')
_dashboard    = os.path.join(PROJECT_DIR, 'live_dashboard.html')
_status_path  = os.path.join(PROJECT_DIR, 'sim_status.json')

# Write initial status so the dashboard shows "LIVE step 0 / STOP" immediately
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

    fig = make_subplots(rows=3, cols=1, shared_xaxes=True,
        subplot_titles=[
            '|V| Bus Voltages (kV) vs LIM iteration',
            'Voltage Angle (deg) vs LIM iteration',
            'Line Active Power Losses (MW)',
        ])

    # Row 1: bus voltage magnitudes — all substations from LINE edges
    for _sub_name in _all_line_subs:
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

    fig.update_xaxes(title_text='LIM iteration', row=3, col=1)
    fig.update_yaxes(title_text='|V| (kV)', row=1, col=1)
    fig.update_yaxes(title_text='angle (deg)', row=2, col=1)
    fig.update_yaxes(title_text='P_loss (MW)', row=3, col=1)
    fig.update_layout(title_text='LIM Co-simulation: MV Network Power Flow', hovermode='x unified')
    fig.show()

except FileNotFoundError:
    print('Warning: output.json not found. Run simulation first.')
except Exception as e:
    import traceback
    print(f'Visualization error: {e}')
    traceback.print_exc()

