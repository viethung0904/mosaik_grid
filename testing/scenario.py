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

Network data (CIGRE MV benchmark, simplified):
    Line_AB: r=2.190 Ω,  x=1.380 Ω  (from Sub_A to Sub_B)
    Sub_A loads: 0.5+0.3 = 0.8 MW,  0.25+0.15 = 0.4 MVAr
    Sub_B loads: 0.432+0.275 = 0.707 MW,  0.108+0.1 = 0.208 MVAr
"""
import cmath
import math
import os
import sys
import json

import mosaik
import plotly.graph_objects as go
from plotly.subplots import make_subplots

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(SCRIPT_DIR, 'simulator'))
sys.path.insert(0, SCRIPT_DIR)

FMU_DIR = os.path.join(os.path.dirname(SCRIPT_DIR), 'fmus')

# ── Network parameters ────────────────────────────────────────────────────────
# Line impedances [Ω]
R_AB, X_AB, BCH_AB = 2.190, 1.380, 0.0   # sub_A  → sub_B

# Self-admittances for each load bus: Y_self = Σ(1/Z_branch) [S]
_Z_AB = complex(R_AB, X_AB)
# Sub_A is adjacent to Line_AB only (Line_A removed)
Y_SELF_RE_A = (1 / _Z_AB).real
Y_SELF_IM_A = (1 / _Z_AB).imag
# Sub_B is adjacent to Line_AB only
Y_SELF_RE_B = (1 / _Z_AB).real
Y_SELF_IM_B = (1 / _Z_AB).imag

# MV nominal voltage [kV] — LV side of 110/20 kV transformer
V_MV_KV = 20.0

# Load values [MW, MVAr]
LOAD_A1 = (0.500, 0.250)
LOAD_A2 = (0.300, 0.150)
LOAD_B1 = (0.432, 0.108)
LOAD_B2 = (0.275, 0.100)

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
world.connect(sub_B, line_AB, ('V_mag_kv', 'V_to_mag_kv'), time_shifted=True, initial_data={'V_mag_kv': 20.0})
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

