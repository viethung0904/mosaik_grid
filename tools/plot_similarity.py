"""
Rel-MAE bar chart: similarity of Mosaik adaptive co-simulation vs Unified FMU.
One bar per signal, coloured by group, sorted group-first then by value.
"""

import json, os
import numpy as np
import plotly.graph_objects as go

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))

with open(os.path.join(PROJECT_DIR, "output_adaptive.json"))    as f: adap = json.load(f)
with open(os.path.join(PROJECT_DIR, "output_whole_system.json")) as f: ws   = json.load(f)

N_STEPS = 1440
RATIO   = len(adap[next(iter(adap))]) // N_STEPS   # = STOP / 1440 = N_LIM

def adap_series(key):
    d = adap[key]
    full = [d[str(i)] for i in range(len(d))]
    return np.array([full[i] for i in range(0, len(full), RATIO)][:N_STEPS], dtype=float)

def ws_series(key):
    d = ws[key]
    return np.array([d[str(i)] for i in range(N_STEPS)], dtype=float)

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
        for suf in ["mag_kv", "ang_deg"]:
            m[f"V_sub_{pfx}{av}_{suf}"] = (f"V_BUS{bus}_{wv}_{suf}", "Voltage")
    return m

KEY_MAP = {**voltage_map(), **{
    "Battery1_SOC":       ("Bat_SOC",    "Battery"),
    "Battery1_V_volt":    ("Bat_V_volt", "Battery"),
    "Battery1_I_amp":     ("Bat_I_amp",  "Battery"),
    "Battery1_P_load_mw": ("Bat_P_mw",  "Battery"),
    "PV1_V_volt":         ("PV_V_volt",  "PV"),
    "PV1_I_amp":          ("PV_I_amp",   "PV"),
    "PV1_P_W":            ("PV_P_W",     "PV"),
    "PV1_P_load_mw":      ("PV_P_mw",   "PV"),
}}

GROUP_COLORS = {
    "Voltage": "#1f77b4",
    "PV":      "#2ca02c",
    "Battery": "#ff7f0e",
    "Losses":  "#9467bd",
}
GROUP_ORDER = ["Voltage", "Battery", "PV", "Losses"]

import re as _re
def _short_label(ak):
    m = _re.search(r"bus_(\d+)_+[\d.]+_(mag_kv|ang_deg)", ak)
    if m:
        bus, sig = m.group(1), m.group(2)
        return f"B{bus} |V|" if sig == "mag_kv" else f"B{bus} ∠"
    return (ak.replace("Battery1_SOC",       "Bat SOC")
              .replace("Battery1_V_volt",    "Bat V")
              .replace("Battery1_I_amp",     "Bat I")
              .replace("Battery1_P_load_mw", "Bat P")
              .replace("PV1_V_volt",         "PV V")
              .replace("PV1_I_amp",          "PV I")
              .replace("PV1_P_W",            "PV P(W)")
              .replace("PV1_P_load_mw",      "PV P"))

# ── Compute Rel-MAE and R² per signal ────────────────────────────────────────
records = []   # {label, group, rel_mae, r2}

for ak, (wk, grp) in KEY_MAP.items():
    try:
        a = adap_series(ak)
        w = ws_series(wk)
    except (KeyError, IndexError):
        continue
    ref = np.abs(w).mean()
    if ref < 1e-12:
        continue
    mae     = np.abs(a - w).mean()
    rel_mae = mae / ref * 100
    ss_res  = np.sum((a - w) ** 2)
    ss_tot  = np.sum((w - w.mean()) ** 2)
    r2      = 1.0 - ss_res / ss_tot if ss_tot > 1e-12 else 1.0
    short   = _short_label(ak)
    records.append(dict(label=short, group=grp, rel_mae=rel_mae, r2=r2))

# Losses
def sum_loss(keys):
    total = np.zeros(N_STEPS)
    for k in keys:
        total += adap_series(k)
    return total

P_loss_keys  = [k for k in adap if k.startswith("P_loss_")]
Q_loss_keys  = [k for k in adap if k.startswith("Q_loss_")]
P_tr_keys    = [k for k in adap if k.startswith("P_loss_branch_")]
Q_tr_keys    = [k for k in adap if k.startswith("Q_loss_branch_")]
P_line_keys  = [k for k in adap if k.startswith("P_loss_line_")]
Q_line_keys  = [k for k in adap if k.startswith("Q_loss_line_")]

def sum_ws(keys):
    total = np.zeros(N_STEPS)
    for k in keys:
        total += ws_series(k)
    return total

for label, a_vals, w_vals in [
    ("P loss total",       sum_loss(P_loss_keys),  sum_ws(P_loss_keys)),
    ("Q loss total",       sum_loss(Q_loss_keys),  sum_ws(Q_loss_keys)),
    ("P loss transformer", sum_loss(P_tr_keys),    sum_ws(P_tr_keys)),
    ("Q loss transformer", sum_loss(Q_tr_keys),    sum_ws(Q_tr_keys)),
    ("P loss line",        sum_loss(P_line_keys),  sum_ws(P_line_keys)),
    ("Q loss line",        sum_loss(Q_line_keys),  sum_ws(Q_line_keys)),
]:
    w = w_vals
    ref = np.abs(w).mean()
    if ref < 1e-12:
        continue
    mae     = np.abs(a_vals - w).mean()
    rel_mae = mae / ref * 100
    ss_res  = np.sum((a_vals - w) ** 2)
    ss_tot  = np.sum((w - w.mean()) ** 2)
    r2      = 1.0 - ss_res / ss_tot if ss_tot > 1e-12 else 1.0
    records.append(dict(label=label, group="Losses", rel_mae=rel_mae, r2=r2))

# Keep only top 3 per group by Rel-MAE, then sort group-first then descending
from collections import defaultdict as _dd
_by_group = _dd(list)
for r in records: _by_group[r["group"]].append(r)
records = []
for grp in GROUP_ORDER:
    top3 = sorted(_by_group.get(grp, []), key=lambda r: -r["rel_mae"])[:3]
    records.extend(top3)

labels   = [r["label"]   for r in records]
rel_maes = [r["rel_mae"] for r in records]
r2s      = [r["r2"]      for r in records]
colors   = [GROUP_COLORS.get(r["group"], "#888") for r in records]

# ── Build figure ──────────────────────────────────────────────────────────────
fig = go.Figure()

fig.add_trace(go.Bar(
    x=labels,
    y=rel_maes,
    marker_color=colors,
    text=[f"{v:.2f}%" for v in rel_maes],
    textposition="outside",
    textfont=dict(size=40),
    customdata=[[r["r2"], r["group"]] for r in records],
    hovertemplate=(
        "<b>%{x}</b><br>"
        "Group: %{customdata[1]}<br>"
        "Rel. MAE: %{y:.6f}%<br>"
        "R²: %{customdata[0]:.6f}"
        "<extra></extra>"
    ),
    showlegend=False,
))

# Invisible legend bars for group colours
for grp, color in GROUP_COLORS.items():
    if any(r["group"] == grp for r in records):
        fig.add_trace(go.Bar(
            x=[None], y=[None],
            name=grp,
            marker_color=color,
            showlegend=True,
        ))

# Reference lines
fig.add_hline(y=0.5,  line=dict(color="orange", width=1, dash="dot"))
fig.add_hline(y=1.0,  line=dict(color="red",    width=1, dash="dot"))

fig.update_layout(
    title=None,
    xaxis=dict(
        title=dict(text="Signal", font=dict(size=20)),
        tickangle=-50,
        tickfont=dict(size=20),
    ),
    yaxis=dict(
        title=dict(text="Relative MAE (%)", font=dict(size=20)),
        tickfont=dict(size=20),
        rangemode="tozero",
    ),
    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1,
                font=dict(size=20), tracegroupgap=10),
    template="plotly_white",
    bargap=0.25,
    height=1080,
    width=1920,
)

out = os.path.join(PROJECT_DIR, "similarity_dashboard.html")
fig.write_html(out)
print(f"Rel-MAE bar chart written to {out}")
