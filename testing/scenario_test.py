"""
Mosaik co-simulation scenario — LIM-based MV network power flow.

Simulation scheme:
    110 kV Slack (V_source) ── TR1 (110/20 kV) ── MV_Slack ── Sub_A ── Line_AB ── Sub_B
                                                                       └── Load_A1          └── Load_B1
                                                                       └── Load_A2          └── Load_B2

Chain (series) topology: slack → sub_A → sub_B  (Line_A removed; MV_Slack feeds Sub_A directly)

LIM mapping:
    Each mosaik timestep = one Jacobi LIM iteration.
    The time_shift=True on Sub→Line connections provides the LIM one-step delay.
    omega_relax=0.5 is required for convergence on radial networks (ρ_eff ≈ 0.5).

Network data sourced from Neo4j GraphDB (CIGRE MV benchmark):
    Transformer: TR1  (110/20 kV)
    Line_AB: N1→N2 (L1-2)  — r, x, bch fetched from graph
    Sub_A loads: Load_1A, Load_1B  — p_mw, q_mvar fetched from graph
    Sub_B loads: Load_2A, Load_2B  — p_mw, q_mvar fetched from graph
"""
import cmath
import math
import os
import sys
import json

from dotenv import load_dotenv
from neo4j import GraphDatabase
import mosaik
import plotly.graph_objects as go
from plotly.subplots import make_subplots

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)          # /home/hungpv/Python_Substation
sys.path.insert(0, os.path.join(SCRIPT_DIR, 'simulator'))  # src/simulator/
sys.path.insert(0, SCRIPT_DIR)

FMU_DIR = os.path.join(PROJECT_DIR, 'fmus')

# ── GraphDB: fetch network parameters ────────────────────────────────────────
def fetch_network_params():
    """Query Neo4j for TR1, Line N1→N2 (L1-2), and loads at N1 and N2."""
    load_dotenv(os.path.join(PROJECT_DIR, '.env'))
    uri  = os.getenv('NEO4J_URI')
    user = os.getenv('NEO4J_USERNAME')
    pwd  = os.getenv('NEO4J_PASSWORD')
    db   = os.getenv('NEO4J_DATABASE')

    driver = GraphDatabase.driver(uri, auth=(user, pwd))
    try:
        with driver.session(database=db) as session:
            # TR1: LV nominal voltage drives the MV slack bus voltage
            tr1 = session.run(
                'MATCH (t:Transformer {name: $name}) RETURN t',
                name='TR1'
            ).single()
            if tr1 is None:
                raise RuntimeError('TR1 not found in graph database')
            tr1_props = dict(tr1['t'])

            # Line N1→N2 (graph name "L1-2")
            line = session.run(
                'MATCH (a:Substation {name:$from_n})-[l:LINE]->(b:Substation {name:$to_n}) '
                'RETURN l',
                from_n='N1', to_n='N2'
            ).single()
            if line is None:
                raise RuntimeError('Line N1→N2 not found in graph database')
            line_props = dict(line['l'])

            # Loads at N1 (Sub_A) ordered by name → Load_1A, Load_1B
            loads_n1 = session.run(
                'MATCH (s:Substation {name:$sub})-[:CONNECT_TO]->(l:Load) '
                'RETURN l.name AS name, l.p_mw AS p_mw, l.q_mvar AS q_mvar '
                'ORDER BY l.name',
                sub='N1'
            ).data()
            if len(loads_n1) < 2:
                raise RuntimeError(f'Expected ≥2 loads at N1, got {len(loads_n1)}')

            # Loads at N2 (Sub_B) ordered by name → Load_2A, Load_2B
            loads_n2 = session.run(
                'MATCH (s:Substation {name:$sub})-[:CONNECT_TO]->(l:Load) '
                'RETURN l.name AS name, l.p_mw AS p_mw, l.q_mvar AS q_mvar '
                'ORDER BY l.name',
                sub='N2'
            ).data()
            if len(loads_n2) < 2:
                raise RuntimeError(f'Expected ≥2 loads at N2, got {len(loads_n2)}')

    finally:
        driver.close()

    return tr1_props, line_props, loads_n1, loads_n2


print('Fetching network parameters from GraphDB...')
_tr1, _line_12, _loads_n1, _loads_n2 = fetch_network_params()
print(f"  TR1 : hv={_tr1['hv_nominal_voltage_kv']} kV / lv={_tr1['lv_nominal_voltage_kv']} kV")
print(f"  L1-2: r={_line_12['r_ohm']} Ω  x={_line_12['x_ohm']} Ω  bch={_line_12['bch']} S")
for _rec in _loads_n1 + _loads_n2:
    print(f"  {_rec['name']}: p={_rec['p_mw']} MW  q={_rec['q_mvar']} MVAr")

# ── Network parameters (sourced from GraphDB) ─────────────────────────────────
# Line N1→N2 impedances [Ω] and shunt susceptance [S]
R_AB   = _line_12['r_ohm']    # was hardcoded 2.190
X_AB   = _line_12['x_ohm']    # was hardcoded 1.380
BCH_AB = _line_12['bch']      # was hardcoded 0.0

# Self-admittances for each load bus: Y_self = Σ(1/Z_branch) [S]
_Z_AB = complex(R_AB, X_AB)
# Sub_A is adjacent to Line_AB only (Line_A removed)
Y_SELF_RE_A = (1 / _Z_AB).real
Y_SELF_IM_A = (1 / _Z_AB).imag
# Sub_B is adjacent to Line_AB only
Y_SELF_RE_B = (1 / _Z_AB).real
Y_SELF_IM_B = (1 / _Z_AB).imag

# MV nominal voltage [kV] — LV side of TR1 from GraphDB
V_MV_KV = _tr1['lv_nominal_voltage_kv']

# Load values [MW, MVAr] — from GraphDB (N1: Load_1A, Load_1B; N2: Load_2A, Load_2B)
LOAD_A1 = (_loads_n1[0]['p_mw'], _loads_n1[0]['q_mvar'])  # Load_1A
LOAD_A2 = (_loads_n1[1]['p_mw'], _loads_n1[1]['q_mvar'])  # Load_1B
LOAD_B1 = (_loads_n2[0]['p_mw'], _loads_n2[0]['q_mvar'])  # Load_2A
LOAD_B2 = (_loads_n2[1]['p_mw'], _loads_n2[1]['q_mvar'])  # Load_2B

# Simulation length = number of LIM iterations
STOP = 20

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

# ── HV side: V_source and Transformer (for monitoring) ───────────────────────
v_source_sim = world.start('V_source',
    fmu_filename=os.path.join(FMU_DIR, 'V_source.fmu'),
    instance_name='V_source', step_size=1)
v_source = v_source_sim.V_source.create(1)[0]

transformer_sim = world.start('Transformer',
    fmu_filename=os.path.join(FMU_DIR, 'TR1.fmu'),
    instance_name='TR1', step_size=1)
transformer = transformer_sim.Transformer.create(1)[0]

# ── MV side: substations ─────────────────────────────────────────────────────
# Start the MV slack bus in its own simulator instance (avoids cycle with load buses)
slack_sim = world.start('Substation',
    fmu_filename=os.path.join(FMU_DIR, 'Substation.fmu'),
    instance_name='Substation_slack', step_size=1)

mv_slack = slack_sim.Substation.create(
    1, is_slack=1.0, V_slack_kv=V_MV_KV)[0]

# Load buses in a separate simulator instance
bus_sim = world.start('Substation',
    fmu_filename=os.path.join(FMU_DIR, 'Substation.fmu'),
    instance_name='Substation_bus', step_size=1)

# Sub_A is now the MV slack (no upstream line — directly at MV_Slack voltage)
sub_A = bus_sim.Substation.create(
    1, is_slack=1.0, V_slack_kv=V_MV_KV)[0]

sub_B = bus_sim.Substation.create(
    1, Y_self_re=Y_SELF_RE_B, Y_self_im=Y_SELF_IM_B,
    B_shunt=0.0, omega_relax=0.5, is_slack=0.0, V_slack_kv=V_MV_KV)[0]

# ── MV side: lines ────────────────────────────────────────────────────────────
line_sim = world.start('ACLineSegment',
    fmu_filename=os.path.join(FMU_DIR, 'ACLineSegment.fmu'),
    instance_name='Line', step_size=1)

line_AB = line_sim.Line.create(1, r_ohm=R_AB, x_ohm=X_AB, bch=BCH_AB)[0]

# ── Loads (constant P+jQ per bus) ─────────────────────────────────────────────
load_sim = world.start('Load',
    fmu_filename=os.path.join(FMU_DIR, 'Load.fmu'),
    instance_name='Load', step_size=1)

load_A1 = load_sim.Load.create(1, p_mw=LOAD_A1[0], q_mvar=LOAD_A1[1])[0]
load_A2 = load_sim.Load.create(1, p_mw=LOAD_A2[0], q_mvar=LOAD_A2[1])[0]
load_B1 = load_sim.Load.create(1, p_mw=LOAD_B1[0], q_mvar=LOAD_B1[1])[0]
load_B2 = load_sim.Load.create(1, p_mw=LOAD_B2[0], q_mvar=LOAD_B2[1])[0]

# ── Collector ─────────────────────────────────────────────────────────────────
collector_sim = world.start('Collector')
collector = collector_sim.Monitor()

# ── HV connections ────────────────────────────────────────────────────────────
world.connect(v_source, transformer, ('V_source_mag', 'V1_mag'))
world.connect(v_source, transformer, ('V_source_angle', 'V1_angle'))

# ── Line_AB connections (Sub_A → Sub_B) ───────────────────────────────────────
# Sub_A is the slack — voltage is constant, so time_shifted initial_data is exact
world.connect(sub_A, line_AB, ('V_mag_kv', 'V_from_mag_kv'), time_shifted=True, initial_data={'V_mag_kv': V_MV_KV})
world.connect(sub_A, line_AB, ('V_ang_deg', 'V_from_ang_deg'), time_shifted=True, initial_data={'V_ang_deg': 0.0})

# To-bus: Sub_B provides its voltage with 1-step LIM delay
world.connect(sub_B, line_AB, ('V_mag_kv', 'V_to_mag_kv'), time_shifted=True, initial_data={'V_mag_kv': V_MV_KV})
world.connect(sub_B, line_AB, ('V_ang_deg', 'V_to_ang_deg'), time_shifted=True, initial_data={'V_ang_deg': 0.0})

# Sub_B net current: I_to from Line_AB (rectangular — matches Substation FMU I_in_re/im)
world.connect(line_AB, sub_B, ('I_to_re', 'I_in_re'))
world.connect(line_AB, sub_B, ('I_to_im', 'I_in_im'))

# ── Load connections (summed automatically by substation_simulator.step()) ────
world.connect(load_A1, sub_A, ('P_load_mw', 'P_load_mw'))
world.connect(load_A1, sub_A, ('Q_load_mvar', 'Q_load_mvar'))
world.connect(load_A2, sub_A, ('P_load_mw', 'P_load_mw'))
world.connect(load_A2, sub_A, ('Q_load_mvar', 'Q_load_mvar'))

world.connect(load_B1, sub_B, ('P_load_mw', 'P_load_mw'))
world.connect(load_B1, sub_B, ('Q_load_mvar', 'Q_load_mvar'))
world.connect(load_B2, sub_B, ('P_load_mw', 'P_load_mw'))
world.connect(load_B2, sub_B, ('Q_load_mvar', 'Q_load_mvar'))

# ── Collector connections ─────────────────────────────────────────────────────
# Substation voltages (main outputs of LIM)
world.connect(mv_slack, collector, ('V_mag_kv', 'V_slack_mag_kv'))
world.connect(sub_A,    collector, ('V_mag_kv', 'V_sub_A_mag_kv'))
world.connect(sub_A,    collector, ('V_ang_deg', 'V_sub_A_ang_deg'))
world.connect(sub_B,    collector, ('V_mag_kv', 'V_sub_B_mag_kv'))
world.connect(sub_B,    collector, ('V_ang_deg', 'V_sub_B_ang_deg'))

# Line losses
world.connect(line_AB, collector, ('P_loss_mw',   'P_loss_AB_mw'))
world.connect(line_AB, collector, ('Q_loss_mvar',  'Q_loss_AB_mvar'))

# HV monitoring
world.connect(v_source,    collector, ('V_source_mag',   'V_HV_mag'))
world.connect(transformer, collector, ('V2', 'V_TR_LV'))

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

    # Row 1: bus voltage magnitudes
    for label, key, color in [
        ('MV Slack',  'V_slack_mag_kv', 'gray'),
        ('Sub A',     'V_sub_A_mag_kv', 'blue'),
        ('Sub B',     'V_sub_B_mag_kv', 'red'),
    ]:
        t, v = series(key)
        fig.add_trace(go.Scatter(x=t, y=v, name=label, line=dict(color=color)), row=1, col=1)

    # Row 2: voltage angles
    for label, key, color in [
        ('Sub A angle', 'V_sub_A_ang_deg', 'blue'),
        ('Sub B angle', 'V_sub_B_ang_deg', 'red'),
    ]:
        t, v = series(key)
        fig.add_trace(go.Scatter(x=t, y=v, name=label, line=dict(color=color, dash='dot')), row=2, col=1)

    # Row 3: line losses
    for label, key, color in [
        ('P_loss Line_AB', 'P_loss_AB_mw', 'blue'),
    ]:
        t, v = series(key)
        fig.add_trace(go.Scatter(x=t, y=v, name=label, line=dict(color=color, dash='dash')), row=3, col=1)

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

