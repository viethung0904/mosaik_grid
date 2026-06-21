#!/usr/bin/env python3
"""
Compare two power-flow JSON outputs (e.g. `output.json` from the mosaik run
and a snapshot JSON produced by the monolithic NR solver). The script finds
bus voltage magnitude keys ending with `_mag_kv` and their corresponding
angle keys `_ang_deg`, compares the last time sample (or a specific time),
and prints a per-bus and summary report.

Usage examples:
  python tools/compare_nr_results.py --ref output.json --cmp output_NR.json

The script expects both files use the same naming convention for monitored
variables (the repository's `collector` naming, e.g. `V_sub_bus_10__13.8_mag_kv`).
"""
from __future__ import annotations

import argparse
import json
import math
from typing import Dict, Tuple, Any


def load_snapshot_values(data: Dict[str, Any], time: str | int = 'last') -> Dict[str, Tuple[float, float]]:
    """Return mapping label -> (V_mag, V_ang) for the chosen time sample.

    - data: loaded JSON (top-level keys -> dict(time->value) or scalar)
    - time: 'last' (default) or an integer time index (as used in the JSON keys)
    """
    mag_suffix = '_mag_kv'
    ang_suffix = '_ang_deg'
    res: Dict[str, Tuple[float, float]] = {}

    for key, val in data.items():
        if not key.endswith(mag_suffix):
            continue
        ang_key = key[:-len(mag_suffix)] + ang_suffix
        if ang_key not in data:
            continue

        mag_series = data[key]
        ang_series = data[ang_key]

        # choose sample
        if isinstance(mag_series, dict):
            if time == 'last':
                t = sorted(mag_series.keys(), key=int)[-1]
            else:
                t = str(int(time))
            try:
                mag = float(mag_series[t])
                ang = float(ang_series[t])
            except Exception:
                continue
        else:
            mag = float(mag_series)
            ang = float(ang_series)

        label = key[:-len(mag_suffix)]
        res[label] = (mag, ang)

    return res


def wrap_angle_diff(a: float, b: float) -> float:
    d = a - b
    d = (d + 180.0) % 360.0 - 180.0
    return d


def compare(ref: Dict[str, Tuple[float, float]], cmp: Dict[str, Tuple[float, float]], tol_pu: float = 1e-3) -> None:
    common = sorted(set(ref.keys()) & set(cmp.keys()))
    if not common:
        print('No matching monitored buses found between the two files.')
        return

    print(f'Comparing {len(common)} monitored buses (last sample).')
    print(f"{'Bus':30} {'V_ref':>8} {'V_cmp':>8} {'ΔV':>8} {'ΔV(%)':>8} {'θ_ref':>8} {'θ_cmp':>8} {'Δθ':>8}")

    max_dv = 0.0
    max_dth = 0.0
    worst_bus = None

    for label in common:
        v_ref, th_ref = ref[label]
        v_cmp, th_cmp = cmp[label]
        dv = v_ref - v_cmp
        dv_abs = abs(dv)
        dv_rel = (dv_abs / v_ref) if abs(v_ref) > 1e-12 else float('inf')
        dth = wrap_angle_diff(th_ref, th_cmp)

        print(f'{label:30} {v_ref:8.4f} {v_cmp:8.4f} {dv_abs:8.4f} {dv_rel*100:8.3f}% {th_ref:8.3f} {th_cmp:8.3f} {dth:8.3f}')

        if dv_abs > max_dv:
            max_dv = dv_abs
            worst_bus = label
        if abs(dth) > max_dth:
            max_dth = abs(dth)

    print('\nSummary:')
    print(f'  Max |ΔV| = {max_dv:.6f} (units same as input, e.g. kV)')
    print(f'  Max |Δθ| = {max_dth:.6f} deg')
    if worst_bus:
        print(f'  Worst bus by |ΔV|: {worst_bus}')


def main() -> None:
    parser = argparse.ArgumentParser(description='Compare two power-flow JSON outputs')
    parser.add_argument('--ref', required=True, help='Reference JSON (e.g. output.json)')
    parser.add_argument('--cmp', required=True, help='Comparison JSON (e.g. output_NR.json)')
    parser.add_argument('--time', default='last', help='Time sample index to compare (default: last)')
    parser.add_argument('--tol-pu', type=float, default=1e-3, help='Voltage tolerance in p.u. (informational)')
    args = parser.parse_args()

    with open(args.ref, 'r') as f:
        ref_data = json.load(f)
    with open(args.cmp, 'r') as f:
        cmp_data = json.load(f)

    ref_vals = load_snapshot_values(ref_data, args.time)
    cmp_vals = load_snapshot_values(cmp_data, args.time)

    compare(ref_vals, cmp_vals, tol_pu=args.tol_pu)


if __name__ == '__main__':
    main()
