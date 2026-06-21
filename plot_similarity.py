"""
Similarity dashboard: Unified FMU (NR solver) vs Adaptive co-simulation.
Three panels:
  1. Parity scatter   — every (adaptive, unified) point; y=x = perfect match
  2. Error heatmap    — 38 signals × 1440 steps, colour = |adaptive − unified|
  3. Rel-MAE bar chart — per signal, coloured by group
"""

import json, csv, os
import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))

# ── Load raw outputs ──────────────────────────────────────────────────────────
with open(os.path.join(PROJECT_DIR, "output_adaptive.json"))    as f: adap = json.load(f)
with open(os.path.join(PROJECT_DIR, "output_whole_system.json")) as f: ws   = json.load(f)

RATIO    = 5
N_STEPS  = 1440
time_h   = [i * 60 / 3600 for i in range(N_STEPS)]

def adap_series(key):
    d = adap[key]
    full = [d[str(i)] for i in range(len(d))]
    return [full[i] for i in range(0, len(full), RATIO)][:N_STEPS]

def ws_series(key):
    d = ws[key]
    return [d[str(i)] for i in range(N_STEPS)]

# ── Build key mapping (mirrors compare_outputs.py) ───────────────────────────
def voltage_map():
    m = {}
    bus_info = [
        (1,"69.0","69"),(2,"69.0","69"),(3,"69.0","69"),(4,"69.0","69"),
        (5,"69.0","69"),(6,"13.8","138"),(7,"13.8","138"),(8,"18.0","18"),
        (9,"13.8","138"),(10,"13.8","138"),(11,"13.8","138"),
        (12,"13.8","138"),(13,"13.8","138"),(14,"13.8","138"),
    ]
    for bus, av, wv in bus_info:
        pfx = f"bus_{bus}___" if bus < 10 else f"bus_{bus}__"
        for suf, wsuf in [("mag_kv","mag_kv"),("ang_deg","ang_deg")]:
            m[f"V_sub_{pfx}{av}_{suf}"] = (f"V_BUS{bus}_{wv}_{wsuf}", "Voltage")
    return m

KEY_MAP = {**voltage_map(), **{
    "Battery1_SOC":       ("Bat_SOC",      "Battery"),
    "Battery1_V_volt":    ("Bat_V_volt",   "Battery"),
    "Battery1_I_amp":     ("Bat_I_amp",    "Battery"),
    "Battery1_P_load_mw": ("Bat_P_mw",     "Battery"),
    "PV1_V_volt":         ("PV_V_volt",    "PV"),
    "PV1_I_amp":          ("PV_I_amp",     "PV"),
    "PV1_P_W":            ("PV_P_W",       "PV"),
    "PV1_P_load_mw":      ("PV_P_mw",      "PV"),
}}

# Losses: sum adaptives, compare to ws total
P_loss_adap_keys = [k for k in adap if k.startswith("P_loss_")]
Q_loss_adap_keys = [k for k in adap if k.startswith("Q_loss_")]

def sum_loss(keys):
    total = [0.0] * N_STEPS
    for k in keys:
        s = adap_series(k)
        for i in range(N_STEPS):
            total[i] += s[i]
    return total

# ── Collect per-signal series ─────────────────────────────────────────────────
GROUP_COLORS = {
    "Voltage": "#1f77b4",
    "PV":      "#2ca02c",
    "Battery": "#ff7f0e",
    "Losses":  "#9467bd",
}

signals = []   # {label, group, a_vals, w_vals}

for ak, (wk, grp) in KEY_MAP.items():
    try:
        a = adap_series(ak)
        w = ws_series(wk)
        short = ak.replace("V_sub_","").replace("Battery1_","").replace("PV1_","")
        signals.append(dict(label=short, group=grp, a=a, w=w))
    except (KeyError, IndexError):
        pass

# Losses
p_adap = sum_loss(P_loss_adap_keys)
p_ws   = ws_series("Total_P_loss_mw")
q_adap = sum_loss(Q_loss_adap_keys)
q_ws   = ws_series("Total_Q_loss_mvar")
signals.append(dict(label="P_loss_total (MW)",  group="Losses", a=p_adap, w=p_ws))
signals.append(dict(label="Q_loss_total (MVAr)", group="Losses", a=q_adap, w=q_ws))

# Sort by group for the heatmap
order = ["Voltage", "PV", "Battery", "Losses"]
signals.sort(key=lambda s: (order.index(s["group"]), s["label"]))

# ── 1. PARITY SCATTER ─────────────────────────────────────────────────────────
parity_traces = {}
for sig in signals:
    g = sig["group"]
    if g not in parity_traces:
        parity_traces[g] = dict(x=[], y=[], name=g,
                                 mode="markers",
                                 marker=dict(color=GROUP_COLORS[g], size=3, opacity=0.4),
                                 legendgroup=g)
    parity_traces[g]["x"].extend(sig["a"])
    parity_traces[g]["y"].extend(sig["w"])

# ── 2. ERROR HEATMAP ──────────────────────────────────────────────────────────
labels = [s["label"] for s in signals]
err_matrix = np.array([
    [abs(s["a"][t] - s["w"][t]) for t in range(N_STEPS)]
    for s in signals
], dtype=float)

# Normalise each row by its max so all signals are visible
row_max = err_matrix.max(axis=1, keepdims=True)
row_max[row_max == 0] = 1.0
err_norm = err_matrix / row_max   # 0–1 per signal

# ── 3. REL-MAE BAR ────────────────────────────────────────────────────────────
bar_vals, bar_labels, bar_colors = [], [], []
for sig in signals:
    a, w = np.array(sig["a"]), np.array(sig["w"])
    err  = np.abs(a - w)
    ref  = np.abs(w).mean()
    rel  = (err.mean() / ref * 100) if ref > 1e-10 else 0.0
    bar_vals.append(rel)
    bar_labels.append(sig["label"])
    bar_colors.append(GROUP_COLORS[sig["group"]])

# ── Build figure ──────────────────────────────────────────────────────────────
fig = make_subplots(
    rows=2, cols=2,
    subplot_titles=(
        "Parity plot — Adaptive vs Unified FMU",
        "Normalised absolute error heatmap (38 signals × 1440 steps)",
        "Relative MAE per signal (%)",
        "",
    ),
    column_widths=[0.38, 0.62],
    row_heights=[0.55, 0.45],
    specs=[
        [{"type": "scatter"}, {"type": "heatmap"}],
        [{"type": "bar",  "colspan": 2}, None],
    ],
    vertical_spacing=0.12,
    horizontal_spacing=0.06,
)

# Panel 1 — parity
shown = set()
for g, tr in parity_traces.items():
    a_all = np.array(tr["x"])
    w_all = np.array(tr["y"])
    fig.add_trace(go.Scatter(
        x=tr["x"], y=tr["y"],
        mode="markers",
        name=g,
        marker=dict(color=GROUP_COLORS[g], size=3, opacity=0.35),
        legendgroup=g,
        showlegend=(g not in shown),
    ), row=1, col=1)
    shown.add(g)

# y = x reference line
all_vals = [v for tr in parity_traces.values() for v in tr["x"] + tr["y"]]
mn, mx = min(all_vals), max(all_vals)
fig.add_trace(go.Scatter(
    x=[mn, mx], y=[mn, mx],
    mode="lines",
    line=dict(color="black", width=1.5, dash="dash"),
    name="y = x (perfect)",
    showlegend=True,
), row=1, col=1)
fig.update_xaxes(title_text="Adaptive co-simulation", row=1, col=1)
fig.update_yaxes(title_text="Unified FMU (NR)", row=1, col=1)

# Panel 2 — heatmap
# Group colour bands on y-axis ticks
tick_colors = [GROUP_COLORS[s["group"]] for s in signals]
fig.add_trace(go.Heatmap(
    z=err_norm,
    x=time_h,
    y=labels,
    colorscale="YlOrRd",
    zmin=0, zmax=1,
    colorbar=dict(title="Norm. |err|", len=0.45, y=0.77),
    showscale=True,
    hovertemplate="Time: %{x:.2f} h<br>Signal: %{y}<br>Norm err: %{z:.3f}<extra></extra>",
), row=1, col=2)
fig.update_xaxes(title_text="Time (h)", row=1, col=2)
fig.update_yaxes(tickfont=dict(size=9), row=1, col=2)

# Panel 3 — bar
fig.add_trace(go.Bar(
    x=bar_labels,
    y=bar_vals,
    marker_color=bar_colors,
    showlegend=False,
    hovertemplate="%{x}<br>Rel. MAE: %{y:.4f}%<extra></extra>",
), row=2, col=1)
fig.update_xaxes(tickangle=-55, tickfont=dict(size=8), row=2, col=1)
fig.update_yaxes(title_text="Rel. MAE (%)", row=2, col=1)

# Legend patches for bar colours
for g, c in GROUP_COLORS.items():
    fig.add_trace(go.Bar(x=[None], y=[None], name=g,
                         marker_color=c, legendgroup=g,
                         showlegend=(g not in shown)), row=2, col=1)

fig.update_layout(
    title=dict(
        text="Simulation Similarity: Adaptive Co-simulation vs Unified FMU (Newton-Raphson)",
        font=dict(size=17),
    ),
    height=1000,
    template="plotly_white",
    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    bargap=0.15,
)

out = os.path.join(PROJECT_DIR, "similarity_dashboard.html")
fig.write_html(out)
print(f"Dashboard written to {out}")
