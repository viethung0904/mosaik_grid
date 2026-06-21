"""
run_unified_fmu.py — FMPy driver for IEEE14_NR_PV_Battery_Unified.fmu

Simulates 24 h (1440 physical steps × 60 s = 86 400 s) without mosaik.
Weather inputs S [W/m²] and T [K] are read from input_data.csv at each step.
All 40 FMU outputs are collected and saved to output_whole_system.json in the
same format as the mosaik Collector (key → {str(step): value}).

Usage
─────
  cd /home/hungpv/mosaik_grid
  /home/hungpv/miniforge3/envs/mosaik/bin/python3 src/run_unified_fmu.py

  Optional flags (pass as env-vars or edit constants below):
    STEP_SIZE_S   physical time-step in seconds  (default 60 = 1 min)
    N_STEPS       number of steps                (default 1440 = 24 h)
    P_CHARGE_MW   battery charge setpoint [MW]   (default 0.030)
    PV_SCALE      PV output multiplier            (default 10.0)
"""

import csv
import json
import math
import os
import sys
import time as _walltime

from dotenv import load_dotenv
from fmpy import read_model_description, extract
from fmpy.fmi2 import FMU2Slave

# ── Configuration ─────────────────────────────────────────────────────────────
SCRIPT_DIR   = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR  = os.path.dirname(SCRIPT_DIR)

# Load .env so Neo4j credentials are in os.environ before the FMU calls os.getenv()
# (the FMU runs in the same process but may not find .env from its temp directory)
load_dotenv(os.path.join(PROJECT_DIR, '.env'))

FMU_PATH     = os.path.join(PROJECT_DIR, 'fmus', 'IEEE14_NR_PV_Battery_Unified.fmu')
CSV_PATH     = os.path.join(PROJECT_DIR, 'input_data.csv')
OUTPUT_PATH  = os.environ.get('OUTPUT_PATH',
               os.path.join(PROJECT_DIR, 'output_whole_system.json'))

STEP_SIZE_S  = float(os.environ.get('STEP_SIZE_S', 60))    # 60 s per physical step
N_STEPS      = int(os.environ.get('N_STEPS',      1440))   # 1440 steps = 24 h
P_CHARGE_MW  = float(os.environ.get('P_CHARGE_MW', 0.030)) # battery setpoint [MW]
PV_SCALE     = float(os.environ.get('PV_SCALE',    10.0))  # PV multiplier

STOP_TIME    = N_STEPS * STEP_SIZE_S   # 86 400 s

# ── Load CSV weather data ─────────────────────────────────────────────────────
print(f'Loading weather data from {CSV_PATH} ...')
_weather: dict[float, tuple[float, float]] = {}   # time_s -> (S, T)
with open(CSV_PATH, newline='') as f:
    reader = csv.DictReader(f)
    for row in reader:
        t_s = float(row['time'])
        _weather[t_s] = (float(row['S']), float(row['T']))

_weather_times = sorted(_weather.keys())
print(f'  {len(_weather_times)} rows, t=[{_weather_times[0]:.0f}, {_weather_times[-1]:.0f}] s')

def _get_ST(t_s: float) -> tuple[float, float]:
    """Return (S, T) for the CSV row whose time is nearest to and <= t_s.
    Falls back to the first row if t_s is before all data.
    """
    # Binary search for the largest t_csv <= t_s
    lo, hi = 0, len(_weather_times) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _weather_times[mid] <= t_s:
            lo = mid
        else:
            hi = mid - 1
    return _weather[_weather_times[lo]]

# ── Prepare FMU ───────────────────────────────────────────────────────────────
print(f'\nLoading FMU: {os.path.basename(FMU_PATH)}')
model_desc  = read_model_description(FMU_PATH)
unzip_dir   = extract(FMU_PATH)

# Build variable-reference (VR) lookup
vrs: dict[str, int] = {v.name: v.valueReference for v in model_desc.modelVariables}

# All output variable names
output_names: list[str] = [
    v.name for v in model_desc.modelVariables if v.causality == 'output'
]

# Restore dots in mosaik-compatible key names (FMU uses underscores for dots)
_VOLT_SUBS = [('_69_0', '_69.0'), ('_13_8', '_13.8'), ('_18_0', '_18.0')]

def _fmu_to_mosaik_key(name: str) -> str:
    """Restore dots in voltage level tokens for mosaik-compatible JSON keys."""
    for fmu_tok, mosaik_tok in _VOLT_SUBS:
        name = name.replace(fmu_tok, mosaik_tok)
    return name

output_key_map: dict[str, str] = {nm: _fmu_to_mosaik_key(nm) for nm in output_names}
# Parameter variable names (p_charge_mw, pv_scale_factor)
param_names: list[str] = [
    v.name for v in model_desc.modelVariables if v.causality == 'parameter'
]

print(f'  {len(output_names)} output variables, {len(param_names)} parameters')
print(f'  Parameters: {param_names}')

# ── Instantiate and initialise ────────────────────────────────────────────────
fmu = FMU2Slave(
    guid=model_desc.guid,
    unzipDirectory=unzip_dir,
    modelIdentifier=model_desc.coSimulation.modelIdentifier,
    instanceName='unified_instance',
)

fmu.instantiate()
fmu.setupExperiment(startTime=0.0, stopTime=STOP_TIME)
fmu.enterInitializationMode()

# Set parameters
if 'p_charge_mw' in vrs:
    fmu.setReal([vrs['p_charge_mw']], [P_CHARGE_MW])
if 'pv_scale_factor' in vrs:
    fmu.setReal([vrs['pv_scale_factor']], [PV_SCALE])

# Set initial inputs (t=0 weather)
S0, T0 = _get_ST(0.0)
fmu.setReal([vrs['S'], vrs['T']], [S0, T0])

fmu.exitInitializationMode()

print(f'\nParameters set:  p_charge_mw={P_CHARGE_MW} MW,  pv_scale_factor={PV_SCALE}')
print(f'Initial weather: S={S0:.1f} W/m²,  T={T0:.2f} K')

# ── Simulation loop ────────────────────────────────────────────────────────────
print(f'\nRunning {N_STEPS} steps × {STEP_SIZE_S:.0f} s = {STOP_TIME/3600:.1f} h ...')
print(f'{"Step":>5}  {"t [h]":>6}  {"S [W/m²]":>9}  {"T [K]":>7}  '
      f'{"V_BUS1 [kV]":>11}  {"V_BUS14 [kV]":>12}  '
      f'{"PV_P [MW]":>9}  {"Bat_SOC [%]":>11}')
print('-' * 90)

results: dict[str, dict[str, float]] = {output_key_map[nm]: {} for nm in output_names}
t_wall_start = _walltime.perf_counter()

for step in range(N_STEPS):
    t_now = step * STEP_SIZE_S

    # Update weather inputs for this step
    S, T = _get_ST(t_now)
    fmu.setReal([vrs['S'], vrs['T']], [S, T])

    # Advance FMU by one physical step — all NR iterations run internally
    fmu.doStep(currentCommunicationPoint=t_now, communicationStepSize=STEP_SIZE_S)

    # Read all outputs
    vals = fmu.getReal([vrs[nm] for nm in output_names])
    step_data = dict(zip(output_names, vals))

    # Store with step index as key (matches mosaik Collector format)
    for name, val in step_data.items():
        results[output_key_map[name]][str(step)] = val

    # Progress row (every 120 steps = 2 h, plus first and last)
    if step % 120 == 0 or step == N_STEPS - 1:
        v1  = step_data.get('V_BUS1_69_mag_kv',   float('nan'))
        v14 = step_data.get('V_BUS14_138_mag_kv',  float('nan'))
        pv  = step_data.get('PV_P_mw',             float('nan'))
        soc = step_data.get('Bat_SOC',             float('nan'))
        print(f'{step:5d}  {t_now/3600:6.2f}  {S:9.1f}  {T:7.2f}  '
              f'{v1:11.4f}  {v14:12.4f}  {pv:9.4f}  {soc:11.2f}')

t_wall = _walltime.perf_counter() - t_wall_start
print(f'\nCompleted {N_STEPS} steps in {t_wall:.1f} s  '
      f'({t_wall / N_STEPS * 1000:.1f} ms/step)')

# ── Terminate FMU ─────────────────────────────────────────────────────────────
fmu.terminate()
fmu.freeInstance()

# ── Save results ──────────────────────────────────────────────────────────────
with open(OUTPUT_PATH, 'w') as f:
    json.dump(results, f, indent=2)
print(f'\nResults saved to {OUTPUT_PATH}')
print(f'  Keys: {len(results)} output variables × {N_STEPS} steps')

# ── Quick validation: steady-state voltages vs CIM reference ─────────────────
print('\n=== Steady-state bus voltages (step 0, base load, no PV/sun) ===')
CIM_REF = {                        # sv_voltage_kv from Neo4j (for reference)
    'V_BUS1_69_mag_kv':   73.14,
    'V_BUS2_69_mag_kv':   72.105,
    'V_BUS3_69_mag_kv':   69.690,
    'V_BUS4_69_mag_kv':   70.869,
    'V_BUS5_69_mag_kv':   71.338,
    'V_BUS6_138_mag_kv':  14.766,
    'V_BUS7_138_mag_kv':  14.460,
    'V_BUS8_18_mag_kv':   19.561,
    'V_BUS9_138_mag_kv':  14.238,
    'V_BUS10_138_mag_kv': 14.231,
    'V_BUS11_138_mag_kv': 14.449,
    'V_BUS12_138_mag_kv': 14.520,
    'V_BUS13_138_mag_kv': 14.451,
    'V_BUS14_138_mag_kv': 14.068,
}
print(f'{"Variable":25} {"FMU [kV]":>10} {"CIM [kV]":>10} {"Err %":>8}')
print('-' * 58)
for nm, cim in sorted(CIM_REF.items()):
    fmu_val = results[nm].get('0', float('nan')) if nm in results else float('nan')
    err_pct = (fmu_val - cim) / cim * 100 if cim != 0 and not math.isnan(fmu_val) else float('nan')
    flag = ' !' if abs(err_pct) > 2.0 else ''
    print(f'{nm:25} {fmu_val:10.4f} {cim:10.4f} {err_pct:7.2f}%{flag}')

# ── Visualisation (Plotly, 4-panel) ──────────────────────────────────────────
print('\nGenerating visualisation ...')
try:
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    bus_mag_keys  = sorted([k for k in output_names if k.endswith('_mag_kv')])
    bus_ang_keys  = sorted([k for k in output_names if k.endswith('_ang_deg')])
    times_h       = [step * STEP_SIZE_S / 3600 for step in range(N_STEPS)]

    def _series(key: str) -> list[float]:
        d = results.get(key, {})
        return [d.get(str(s), float('nan')) for s in range(N_STEPS)]

    fig = make_subplots(
        rows=4, cols=1, shared_xaxes=True,
        subplot_titles=[
            '|V| Bus Voltages (kV) — 24 h',
            'Voltage Angles (deg) — non-slack buses',
            'PV Output Power (MW) and Battery SOC (%)',
            'Total Network Losses (MW) and Battery Power (MW)',
        ],
    )

    # Row 1: bus voltage magnitudes
    for key in bus_mag_keys:
        label = key.replace('_mag_kv', '').replace('V_', '')
        fig.add_trace(go.Scatter(x=times_h, y=_series(key), name=f'|V| {label}',
                                 mode='lines'), row=1, col=1)

    # Row 2: voltage angles (skip obvious slack buses at 0°)
    for key in bus_ang_keys:
        label = key.replace('_ang_deg', '').replace('V_', '')
        ang = _series(key)
        if any(abs(v) > 0.01 for v in ang if not math.isnan(v)):
            fig.add_trace(go.Scatter(x=times_h, y=ang, name=f'∠ {label}',
                                     mode='lines', line=dict(dash='dot')), row=2, col=1)

    # Row 3: PV power + battery SOC
    fig.add_trace(go.Scatter(x=times_h, y=[-v for v in _series('PV_P_mw')],
                              name='PV generation (MW)', mode='lines',
                              line=dict(color='gold')), row=3, col=1)
    fig.add_trace(go.Scatter(x=times_h, y=_series('Bat_SOC'),
                              name='Battery SOC (%)', mode='lines',
                              line=dict(color='green'), yaxis='y5'), row=3, col=1)

    # Row 4: network losses + battery power
    fig.add_trace(go.Scatter(x=times_h, y=_series('Total_P_loss_mw'),
                              name='P_loss (MW)', mode='lines',
                              line=dict(color='red')), row=4, col=1)
    fig.add_trace(go.Scatter(x=times_h, y=_series('Bat_P_mw'),
                              name='Battery P_grid (MW)', mode='lines',
                              line=dict(color='blue', dash='dash')), row=4, col=1)

    fig.update_xaxes(title_text='Physical time (h)', row=4, col=1)
    fig.update_yaxes(title_text='|V| (kV)',   row=1, col=1)
    fig.update_yaxes(title_text='Angle (deg)',row=2, col=1)
    fig.update_yaxes(title_text='Power (MW) / SOC (%)', row=3, col=1)
    fig.update_yaxes(title_text='Power (MW)', row=4, col=1)
    fig.update_layout(
        title='IEEE 14-bus Unified FMU — 24 h quasi-static simulation',
        hovermode='x unified',
        height=1000,
    )
    out_html = os.path.join(PROJECT_DIR, 'output_whole_system.html')
    fig.write_html(out_html)
    print(f'Visualisation saved to {out_html}')

except ImportError:
    print('plotly not available — skipping visualisation')
except Exception as exc:
    import traceback
    print(f'Visualisation error: {exc}')
    traceback.print_exc()
