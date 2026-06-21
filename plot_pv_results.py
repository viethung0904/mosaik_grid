"""
Generate an interactive Plotly chart comparing PV simulation results:
  - Unified FMU scheme  (output_adaptive.json, 7200 mosaik ticks = 1440 physical steps x 5 GJ ticks)
  - Whole-system reference (output_whole_system.json, 1440 steps x 60 s)

X-axis: physical time in hours (0-24 h)
Panels: PV Power (W), PV Voltage (V), PV Current (A)
"""

import json
import os
import plotly.graph_objects as go
from plotly.subplots import make_subplots

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))


def load_series(filepath: str, key: str) -> list:
    with open(filepath) as f:
        data = json.load(f)
    d = data[key]
    return [d[str(i)] for i in range(len(d))]


def downsample(series: list, factor: int) -> list:
    return [series[i] for i in range(0, len(series), factor)]


ADAPTIVE_PATH  = os.path.join(PROJECT_DIR, "output_adaptive.json")
WHOLE_SYS_PATH = os.path.join(PROJECT_DIR, "output_whole_system.json")
OUTPUT_HTML    = os.path.join(PROJECT_DIR, "pv_results.html")

RATIO      = 5       # 7200 adaptive ticks / 1440 physical steps
N_STEPS    = 1440
STEP_SEC   = 60
time_hours = [i * STEP_SEC / 3600 for i in range(N_STEPS)]

signals = [
    {"adap_key": "PV1_P_W",     "ws_key": "PV_P_W",     "label": "PV Power",   "unit": "W", "row": 1},
    {"adap_key": "PV1_V_volt",  "ws_key": "PV_V_volt",  "label": "PV Voltage", "unit": "V", "row": 2},
    {"adap_key": "PV1_I_amp",   "ws_key": "PV_I_amp",   "label": "PV Current", "unit": "A", "row": 3},
]

fig = make_subplots(
    rows=3, cols=1,
    shared_xaxes=True,
    subplot_titles=[f"{s['label']} ({s['unit']})" for s in signals],
    vertical_spacing=0.08,
)

COLORS = {"unified": "#1f77b4", "reference": "#ff7f0e"}

for s in signals:
    adap_raw  = load_series(ADAPTIVE_PATH,  s["adap_key"])
    ws_raw    = load_series(WHOLE_SYS_PATH, s["ws_key"])

    adap_down = downsample(adap_raw, RATIO)[:N_STEPS]
    ws_data   = ws_raw[:N_STEPS]

    row = s["row"]
    show_legend = (row == 1)

    fig.add_trace(
        go.Scatter(
            x=time_hours, y=adap_down,
            name="Unified FMU",
            line=dict(color=COLORS["unified"], width=1.8),
            legendgroup="unified",
            showlegend=show_legend,
        ),
        row=row, col=1,
    )
    fig.add_trace(
        go.Scatter(
            x=time_hours, y=ws_data,
            name="Whole-system (reference)",
            line=dict(color=COLORS["reference"], width=1.8, dash="dash"),
            legendgroup="reference",
            showlegend=show_legend,
        ),
        row=row, col=1,
    )
    fig.update_yaxes(title_text=f"{s['label']} ({s['unit']})", row=row, col=1)

fig.update_xaxes(title_text="Time (h)", row=3, col=1)
fig.update_layout(
    title=dict(
        text="PV Simulation Results — Unified FMU vs Whole-System Reference",
        font=dict(size=18),
    ),
    height=800,
    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    template="plotly_white",
    hovermode="x unified",
)

fig.write_html(OUTPUT_HTML)
print(f"Chart written to {OUTPUT_HTML}")
