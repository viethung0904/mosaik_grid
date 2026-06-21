"""
Compare output_adaptive.json vs output_whole_system.json

- adaptive:    7200 timesteps (5 GJ ticks × 1440 physical steps over 24 h)
- whole_system: 1440 timesteps (1 step × 60 s over 24 h)
  → downsample ratio: 7200 / 1440 = 5

Key-name mapping between the two files is defined in KEY_MAP below.
"""

import json
import math
import csv
import os
import sys
import argparse

# ---------------------------------------------------------------------------
# Key mapping:  adaptive_key  ->  whole_system_key
# Bus voltages: bus_X___VV.V  ->  BUSX_VV (13.8 becomes 138, 69.0 -> 69, 18.0 -> 18)
# ---------------------------------------------------------------------------
def build_voltage_map():
    """Auto-generate voltage key mappings for all 14 buses."""
    mapping = {}
    # (bus_num, adaptive_voltage_str, whole_system_voltage_str)
    bus_info = [
        (1,  "69.0", "69"),
        (2,  "69.0", "69"),
        (3,  "69.0", "69"),
        (4,  "69.0", "69"),
        (5,  "69.0", "69"),
        (6,  "13.8", "138"),
        (7,  "13.8", "138"),
        (8,  "18.0", "18"),
        (9,  "13.8", "138"),
        (10, "13.8", "138"),
        (11, "13.8", "138"),
        (12, "13.8", "138"),
        (13, "13.8", "138"),
        (14, "13.8", "138"),
    ]
    for bus, av, wv in bus_info:
        # adaptive uses triple-underscore for single-digit buses (1-9)
        # and double-underscore for double-digit buses (10-14)
        if bus < 10:
            a_bus = f"bus_{bus}___{av}"
        else:
            a_bus = f"bus_{bus}__{av}"

        for suffix in ("mag_kv", "ang_deg"):
            a_key = f"V_sub_{a_bus}_{suffix}"
            w_key = f"V_BUS{bus}_{wv}_{suffix}"
            mapping[a_key] = w_key
    return mapping


VOLTAGE_MAP = build_voltage_map()

SCALAR_MAP = {
    # Battery
    "Battery1_SOC":       "Bat_SOC",
    "Battery1_V_volt":    "Bat_V_volt",
    "Battery1_I_amp":     "Bat_I_amp",
    "Battery1_P_load_mw": "Bat_P_mw",
    # PV
    "PV1_V_volt":         "PV_V_volt",
    "PV1_I_amp":          "PV_I_amp",
    "PV1_P_W":            "PV_P_W",
    "PV1_P_load_mw":      "PV_P_mw",
}

KEY_MAP = {**VOLTAGE_MAP, **SCALAR_MAP}

# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------

def series_to_list(d: dict) -> list:
    """Convert {"0": v0, "1": v1, ...} to a plain list sorted by integer index."""
    return [d[str(i)] for i in range(len(d))]


def downsample(series: list, factor: int) -> list:
    """Take every `factor`-th element starting at index 0."""
    return [series[i] for i in range(0, len(series), factor)]


def compare_series(a_series: list, w_series: list, ratio: int):
    """Downsample adaptive by ratio, then compute element-wise error statistics."""
    a_down = downsample(a_series, ratio)
    length = min(len(a_down), len(w_series))
    errors = [a_down[i] - w_series[i] for i in range(length)]
    n = length
    mae  = sum(abs(e) for e in errors) / n
    rmse = math.sqrt(sum(e ** 2 for e in errors) / n)
    max_err = max(abs(e) for e in errors)
    mean_ref = sum(w_series[:length]) / n
    rel_mae = (mae / abs(mean_ref) * 100) if mean_ref != 0 else None
    return {
        "n": n,
        "mae": mae,
        "rmse": rmse,
        "max_abs_err": max_err,
        "mean_ref": mean_ref,
        "rel_mae": rel_mae,
        "a_down": a_down[:length],
        "w_vals": w_series[:length],
    }


def _aggregate_series(adaptive: dict, prefixes: list[str], ratio: int) -> list | None:
    """Sum all adaptive series whose keys start with any of the given prefixes,
    then downsample by ratio. Returns None if no matching keys exist."""
    keys = [k for k in adaptive if any(k.startswith(p) for p in prefixes)]
    if not keys:
        return None, []
    n_a = len(next(iter(adaptive.values())))
    agg = [0.0] * n_a
    for k in keys:
        for i, v in enumerate(series_to_list(adaptive[k])):
            agg[i] += v
    return agg, keys


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Compare output_adaptive.json vs output_whole_system.json")
    parser.add_argument("adaptive",    nargs="?", default="output_adaptive.json",    help="Path to adaptive output JSON")
    parser.add_argument("whole_system", nargs="?", default="output_whole_system.json", help="Path to whole-system output JSON")
    parser.add_argument("--csv",       default="compare_outputs.csv", help="Output CSV path (default: compare_outputs.csv)")
    args = parser.parse_args()

    print("Loading JSON files...")
    with open(args.adaptive) as f:
        adaptive = json.load(f)
    with open(args.whole_system) as f:
        whole = json.load(f)

    n_a = len(next(iter(adaptive.values())))
    n_w = len(next(iter(whole.values())))
    ratio = n_a // n_w
    print(f"  adaptive    : {n_a} timesteps")
    print(f"  whole_system: {n_w} timesteps")
    print(f"  downsample ratio: {ratio}:1")
    print()

    results = []
    skipped = []

    for a_key, w_key in KEY_MAP.items():
        if w_key is None:
            skipped.append((a_key, "no whole_system counterpart defined"))
            continue
        if a_key not in adaptive:
            skipped.append((a_key, "key missing from output_adaptive.json"))
            continue
        if w_key not in whole:
            skipped.append((a_key, f"'{w_key}' missing from output_whole_system.json"))
            continue

        a_series = series_to_list(adaptive[a_key])
        w_series = series_to_list(whole[w_key])
        s = compare_series(a_series, w_series, ratio)
        results.append({
            "adaptive_key": a_key,
            "whole_key":    w_key,
            "group":        "Voltage" if a_key.startswith("V_sub") else
                            "Battery" if a_key.startswith("Battery") else "PV",
            **{k: v for k, v in s.items() if k not in ("a_down", "w_vals")},
        })

    # ---- Aggregated loss comparison (problem 2) ----------------------------
    # The adaptive outputs per-line and per-branch losses; the whole-system
    # outputs only the totals.  Sum all adaptive loss terms and compare.
    loss_results = []
    for loss_type, a_prefixes, w_key, unit in [
        ("P", ["P_loss_line_", "P_loss_branch_"], "Total_P_loss_mw",   "MW"),
        ("Q", ["Q_loss_line_", "Q_loss_branch_"], "Total_Q_loss_mvar", "MVAr"),
    ]:
        agg, matched_keys = _aggregate_series(adaptive, a_prefixes, ratio)
        if agg is None or w_key not in whole:
            continue
        w_series = series_to_list(whole[w_key])
        s = compare_series(agg, w_series, ratio)
        loss_results.append({
            "adaptive_key": f"Total_{loss_type}_loss_adaptive [{unit}] (Σ {len(matched_keys)} terms)",
            "whole_key":    w_key,
            "group":        "Losses",
            **{k: v for k, v in s.items() if k not in ("a_down", "w_vals")},
        })

    all_results = results + loss_results

    # --- Print results table ---
    col_a = 56
    col_w = 26
    print(
        f"{'Adaptive key':<{col_a}} {'WS key':<{col_w}} "
        f"{'MAE':>12} {'RMSE':>12} {'MaxErr':>12} {'Rel.MAE%':>10} {'RefMean':>12}"
    )
    sep_len = col_a + col_w + 12 * 3 + 10 + 12 + 6
    print("-" * sep_len)

    results_sorted = sorted(all_results, key=lambda r: r.get("rel_mae") or 0, reverse=True)
    for r in results_sorted:
        rel = f"{r['rel_mae']:.4f}" if r["rel_mae"] is not None else "N/A"
        print(
            f"{r['adaptive_key']:<{col_a}} {r['whole_key']:<{col_w}} "
            f"{r['mae']:>12.6f} {r['rmse']:>12.6f} {r['max_abs_err']:>12.6f} "
            f"{rel:>10} {r['mean_ref']:>12.4f}"
        )

    # --- Skipped ---
    if skipped:
        print()
        print("Skipped keys:")
        for k, reason in skipped:
            print(f"  {k}: {reason}")

    # ---- Adaptive-only signals (problem 3) ---------------------------------
    # Per-line currents and per-line/branch losses have no whole-system
    # counterpart for direct comparison.  Report their statistics from the
    # adaptive simulation so they are not silently ignored.
    current_keys = sorted(k for k in adaptive if k.startswith("I_from_line_") or k.startswith("I_to_line_"))
    if current_keys:
        print()
        print("=== Adaptive-only: per-line currents (no whole-system counterpart) ===")
        print(f"  {'Signal':<56} {'Mean [kA]':>12} {'Max|I| [kA]':>12}")
        print(f"  {'-'*80}")
        for k in current_keys:
            s_down = downsample(series_to_list(adaptive[k]), ratio)
            mean_v = sum(s_down) / len(s_down)
            max_v  = max(abs(v) for v in s_down)
            print(f"  {k:<56} {mean_v:>12.4f} {max_v:>12.4f}")

    # ---- Problem 4: PV daytime-only statistics --------------------------------
    # Nighttime steps contribute zero error and zero signal, which halves the
    # reported MAE.  Recompute PV metrics over active steps only.
    pv_pairs = [(ak, wk) for ak, wk in KEY_MAP.items() if ak.startswith("PV")]
    pv_pairs = [(ak, wk) for ak, wk in pv_pairs
                if ak in adaptive and wk in whole]
    if pv_pairs:
        print()
        print("=== PV: active-period-only statistics (steps where either value > 0) ===")
        print(f"  {'Adaptive key':<30} {'Active steps':>12} {'Day MAE':>12} {'Day Rel.MAE%':>14}")
        print(f"  {'-'*68}")
        for ak, wk in sorted(pv_pairs):
            a_s = downsample(series_to_list(adaptive[ak]), ratio)
            w_s = series_to_list(whole[wk])
            length = min(len(a_s), len(w_s))
            active = [i for i in range(length) if abs(a_s[i]) > 0 or abs(w_s[i]) > 0]
            if not active:
                continue
            day_mae = sum(abs(a_s[i] - w_s[i]) for i in active) / len(active)
            mean_w  = sum(abs(w_s[i]) for i in active) / len(active)
            rel     = day_mae / mean_w * 100 if mean_w > 0 else float("nan")
            print(f"  {ak:<30} {len(active):>12d} {day_mae:>12.4f} {rel:>13.4f}%")

    per_line_loss_keys = sorted(
        k for k in adaptive
        if k.startswith("P_loss_line_") or k.startswith("Q_loss_line_")
        or k.startswith("P_loss_branch_") or k.startswith("Q_loss_branch_")
    )
    if per_line_loss_keys:
        print()
        print("=== Adaptive-only: per-line/branch losses (summed totals compared above) ===")
        print(f"  {'Signal':<62} {'Mean':>10} {'Max':>10}  Unit")
        print(f"  {'-'*86}")
        for k in per_line_loss_keys:
            s_down = downsample(series_to_list(adaptive[k]), ratio)
            mean_v = sum(s_down) / len(s_down)
            max_v  = max(abs(v) for v in s_down)
            unit   = "MW" if k.endswith("_mw") else "MVAr"
            print(f"  {k:<62} {mean_v:>10.4f} {max_v:>10.4f}  {unit}")

    # --- Summary by group ---
    print()
    print("=== Summary ===")
    group_names = ["Voltage", "Battery", "PV", "Losses"]
    for group_name in group_names:
        group = [r for r in all_results if r.get("group") == group_name]
        if not group:
            continue
        avg_mae  = sum(r["mae"]  for r in group) / len(group)
        avg_rmse = sum(r["rmse"] for r in group) / len(group)
        rel_maes = [r["rel_mae"] for r in group if r["rel_mae"] is not None]
        avg_rel  = sum(rel_maes) / len(rel_maes) if rel_maes else float("nan")
        max_err  = max(r["max_abs_err"] for r in group)
        print(
            f"  {group_name:10s}: {len(group):2d} signals | "
            f"avg MAE={avg_mae:.6f} | avg RMSE={avg_rmse:.6f} | "
            f"avg Rel.MAE={avg_rel:.4f}% | worst MaxErr={max_err:.6f}"
        )

    # --- Export CSV ---
    csv_path = args.csv
    fieldnames = ["adaptive_key", "whole_key", "group", "n", "mae", "rmse", "max_abs_err", "rel_mae", "mean_ref"]
    with open(csv_path, "w", newline="") as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()
        for r in sorted(all_results, key=lambda x: x["adaptive_key"]):
            writer.writerow({k: r.get(k, "") for k in fieldnames})
    print(f"\nDetailed CSV saved to: {csv_path}")


if __name__ == "__main__":
    main()
