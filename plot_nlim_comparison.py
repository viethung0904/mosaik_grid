"""
Compare voltage-angle Rel-MAE for N_LIM = 1, 3, 5 vs Unified FMU.
Groups bars by signal, with one bar per N_LIM value per signal.
"""

import json, os
import numpy as np
import plotly.graph_objects as go

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))

with open(os.path.join(PROJECT_DIR, "output_whole_system.json")) as f:
    ws = json.load(f)

NLIM_FILES = {
    1: "output_adaptive_nlim1.json",
    3: "output_adaptive_nlim3.json",
    5: "output_adaptive_nlim5.json",
}
NLIM_COLORS = {1: "#d62728", 3: "#ff7f0e", 5: "#2ca02c"}
N_STEPS = 1440

def adap_series(adap, key, ratio):
    d = adap[key]
    full = [d[str(i)] for i in range(len(d))]
    return np.array([full[i] for i in range(0, len(full), ratio)][:N_STEPS], dtype=float)

def ws_series(key):
    d = ws[key]
    return np.array([d[str(i)] for i in range(N_STEPS)], dtype=float)

# Angle signal mapping: adaptive key → unified key
ANGLE_MAP = {}
bus_info = [
    (1,"69.0","69"),(2,"69.0","69"),(3,"69.0","69"),(4,"69.0","69"),
    (5,"69.0","69"),(6,"13.8","138"),(7,"13.8","138"),(8,"18.0","18"),
    (9,"13.8","138"),(10,"13.8","138"),(11,"13.8","138"),
    (12,"13.8","138"),(13,"13.8","138"),(14,"13.8","138"),
]
for bus, av, wv in bus_info:
    pfx = f"bus_{bus}___" if bus < 10 else f"bus_{bus}__"
    ak = f"V_sub_{pfx}{av}_ang_deg"
    wk = f"V_BUS{bus}_{wv}_ang_deg"
    ANGLE_MAP[ak] = (wk, f"Bus {bus}")

# Load all adaptive outputs
adap_data = {}
for nlim, fname in NLIM_FILES.items():
    path = os.path.join(PROJECT_DIR, fname)
    with open(path) as f:
        adap_data[nlim] = json.load(f)

# Compute Rel-MAE per signal per N_LIM
results = {}   # signal_label → {nlim: rel_mae}
for ak, (wk, label) in ANGLE_MAP.items():
    try:
        w = ws_series(wk)
    except KeyError:
        continue
    ref = np.abs(w).mean()
    if ref < 1e-12:
        continue
    results[label] = {}
    for nlim, adap in adap_data.items():
        try:
            a = adap_series(adap, ak, nlim)
            rel_mae = np.abs(a - w).mean() / ref * 100
            results[label][nlim] = rel_mae
        except KeyError:
            pass

# Sort signals by Bus number
labels = sorted(results.keys(), key=lambda s: int(s.split()[1]))

fig = go.Figure()

for nlim in [1, 3, 5]:
    y_vals = [results[lbl].get(nlim, 0.0) for lbl in labels]
    fig.add_trace(go.Bar(
        name=f"N_LIM = {nlim}",
        x=labels,
        y=y_vals,
        marker_color=NLIM_COLORS[nlim],
        text=[f"{v:.3f}%" for v in y_vals],
        textposition="outside",
        textfont=dict(size=18),
    ))

fig.update_layout(
    barmode="group",
    xaxis=dict(
        title=dict(text="Bus", font=dict(size=20)),
        tickfont=dict(size=20),
    ),
    yaxis=dict(
        title=dict(text="Voltage Angle Rel. MAE (%)", font=dict(size=20)),
        tickfont=dict(size=20),
        rangemode="tozero",
    ),
    legend=dict(font=dict(size=20), orientation="h", yanchor="bottom", y=1.02,
                xanchor="right", x=1),
    margin=dict(t=100, b=80),
    template="plotly_white",
    bargap=0.2,
    bargroupgap=0.05,
    height=1080,
    width=1920,
)

out = os.path.join(PROJECT_DIR, "nlim_comparison.html")
fig.write_html(out)
print(f"N_LIM comparison chart written to {out}")
