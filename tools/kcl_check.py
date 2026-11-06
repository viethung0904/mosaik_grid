#!/usr/bin/env python3
"""
Standalone KCL mismatch check at CIM reference voltages.
Does NOT import the scenario module (which runs world.run at module level).

Verifies at each PQ bus:
  I_in_from_pi_lines == I_load_consumed

where I_in_from_pi_lines uses the full ACLineSegment pi-model (series + shunts),
and for HV transformer buses the load includes the scenario's transformer
equivalent load (derived using the same Y-bus formula as _compute_tr_equiv_loads).
"""
import cmath, math, os
from dotenv import load_dotenv
load_dotenv('/home/hungpv/mosaik_grid/.env')
from neo4j import GraphDatabase

# ── 1. Fetch all data from Neo4j ──────────────────────────────────────────────
driver = GraphDatabase.driver(os.getenv('NEO4J_URI'),
                              auth=(os.getenv('NEO4J_USERNAME'), os.getenv('NEO4J_PASSWORD')))
with driver.session(database=os.getenv('NEO4J_DATABASE')) as s:
    subs = {r['name']: r for r in s.run(
        'MATCH (sub:Substation) RETURN sub.name AS name, sub.sv_voltage_kv AS v, '
        'sub.sv_angle_deg AS ang, sub.is_slack AS is_slack, '
        'sub.is_sync_machine AS is_sm, sub.nominal_voltage_kv AS v_nom'
    ).data()}
    line_data = s.run(
        'MATCH (a:Substation)-[l:LINE]->(b:Substation) '
        'RETURN a.name AS fs, b.name AS ts, l.r_ohm AS r, l.x_ohm AS x, l.bch AS bch'
    ).data()
    load_edges = s.run(
        'MATCH (sub:Substation)-[:CONNECT_TO]->(l:Load) '
        'RETURN sub.name AS sub, l.p_mw AS p, l.q_mvar AS q'
    ).data()
    tr_edges = s.run(
        'MATCH (hv:Substation)-[:CONNECT_TO {side:"HV"}]->(t:Transformer)'
        '-[:CONNECT_TO {side:"LV"}]->(lv:Substation) '
        'RETURN hv.name AS hv, lv.name AS lv, t.name AS tr'
    ).data()
driver.close()

# ── 2. Build reference voltage dict ──────────────────────────────────────────
V_ref = {n: cmath.rect(d['v'], math.radians(d['ang'] or 0.0))
         for n, d in subs.items() if d['v'] is not None}

# ── 3. LV-slack buses (transformer secondary buses) treated as fixed ──────────
true_slack = {n for n, d in subs.items() if d.get('is_slack')}
lv_slack   = {tr['lv'] for tr in tr_edges if tr['lv'] not in true_slack}
all_slack  = true_slack | lv_slack
sync_mach  = {n for n, d in subs.items() if d.get('is_sm')}

print(f"True slack: {sorted(true_slack)}")
print(f"LV slack  : {sorted(lv_slack)}")
print(f"Sync mach : {sorted(sync_mach)}")

# ── 4. Direct load totals per bus ─────────────────────────────────────────────
bus_p_load = {}
bus_q_load = {}
for ld in load_edges:
    k = ld['sub']
    bus_p_load[k] = bus_p_load.get(k, 0.0) + (ld['p'] or 0.0)
    bus_q_load[k] = bus_q_load.get(k, 0.0) + (ld['q'] or 0.0)

# ── 5. Transformer equivalent load (Y-bus method — same as _compute_tr_equiv_loads) ─
hv_threshold = 50.0  # kV — HV zone
hv_buses = {n for n, d in subs.items() if (d.get('v_nom') or 0) >= hv_threshold}
hv_tr_buses = ({tr['hv'] for tr in tr_edges} & hv_buses) - {tr['lv'] for tr in tr_edges}

all_hv = sorted(hv_buses)
n_hv   = len(all_hv)
idx    = {b: i for i, b in enumerate(all_hv)}
Y_hv   = [[complex(0)] * n_hv for _ in range(n_hv)]

for line in line_data:
    fs, ts = line['fs'], line['ts']
    if fs not in idx or ts not in idx:
        continue
    Z = complex(line['r'], line['x'])
    y = 1.0 / Z if abs(Z) > 1e-15 else complex(1e6)
    i, j = idx[fs], idx[ts]
    Y_hv[i][i] += y; Y_hv[j][j] += y; Y_hv[i][j] -= y; Y_hv[j][i] -= y

tr_equiv_p = {}; tr_equiv_q = {}
print("\n=== Transformer equivalent loads (Y-bus method) ===")
for hv_bus in sorted(hv_tr_buses):
    i = idx[hv_bus]
    V_hv_bus = V_ref[hv_bus]
    I_inj = sum(Y_hv[i][j] * V_ref[all_hv[j]] for j in range(n_hv))
    S = V_hv_bus * I_inj.conjugate()

    # bch correction for pi-model shunts at this HV bus
    bch_half_sum = sum(
        (line['bch'] or 0) / 2
        for line in line_data
        if (line['fs'] == hv_bus or line['ts'] == hv_bus)
           and line['fs'] in idx and line['ts'] in idx
    )
    bch_q_corr = bch_half_sum * abs(V_hv_bus) ** 2

    p_total = -S.real;      q_total = -S.imag + bch_q_corr
    p_dir   = bus_p_load.get(hv_bus, 0.0)
    q_dir   = bus_q_load.get(hv_bus, 0.0)
    tr_equiv_p[hv_bus] = p_total - p_dir
    tr_equiv_q[hv_bus] = q_total - q_dir
    print(f"  {hv_bus}: P_tr={tr_equiv_p[hv_bus]:.4f} MW, Q_tr={tr_equiv_q[hv_bus]:.4f} MVAr"
          f"  (P_total={p_total:.4f}, Q_total={q_total:.4f}, bch_Q_corr={bch_q_corr:.4f})")

# ── 6. KCL check at every PQ bus ─────────────────────────────────────────────
# PQ buses = not slack, not sync_machine
pq_buses = [n for n in subs
            if n not in all_slack and n not in sync_mach and subs[n]['v'] is not None]

print("\n=== KCL check at all PQ buses ===")
all_ok = True
for bus in sorted(pq_buses):
    V_b = V_ref[bus]

    # Sum pi-model line currents INTO the bus
    I_in = complex(0)
    for line in line_data:
        fs, ts = line['fs'], line['ts']
        Z      = complex(line['r'], line['x'])
        Yh     = complex(0, (line['bch'] or 0) / 2)
        V_f    = V_ref.get(fs, complex(0))
        V_t    = V_ref.get(ts, complex(0))
        I_s    = (V_f - V_t) / Z if abs(Z) > 1e-15 else complex(0)
        I_from = I_s + Yh * V_f   # leaves from-bus
        I_to   = I_s - Yh * V_t   # enters  to-bus
        if ts == bus:
            I_in += I_to          # current entering bus from this line's to-end
        elif fs == bus:
            I_in -= I_from        # -(current leaving bus) = current into bus

    # Expected: direct loads + transformer equivalent
    p_load_total = bus_p_load.get(bus, 0.0) + tr_equiv_p.get(bus, 0.0)
    q_load_total = bus_q_load.get(bus, 0.0) + tr_equiv_q.get(bus, 0.0)
    I_expected   = (p_load_total - 1j * q_load_total) / V_b.conjugate()

    mismatch = I_in - I_expected
    ok = abs(mismatch) < 1e-4
    if not ok:
        all_ok = False
    flag = "(OK)" if ok else "*** MISMATCH ***"

    print(f"\n  {bus}  V={abs(V_b):.5f} kV ∠{math.degrees(cmath.phase(V_b)):.3f}°")
    print(f"    I_in_lines   = {I_in.real:+.6f}+j{I_in.imag:+.6f} kA  |{abs(I_in):.6f}|")
    print(f"    I_expected   = {I_expected.real:+.6f}+j{I_expected.imag:+.6f} kA  |{abs(I_expected):.6f}|")
    print(f"    mismatch |Δ| = {abs(mismatch):.2e} kA  ({100*abs(mismatch)/max(abs(I_expected),1e-9):.4f}%)  {flag}")
    print(f"    P={p_load_total:.4f} MW (dir={bus_p_load.get(bus,0):.4f} tr={tr_equiv_p.get(bus,0):.4f})"
          f"  Q={q_load_total:.4f} MVAr (dir={bus_q_load.get(bus,0):.4f} tr={tr_equiv_q.get(bus,0):.4f})")

print()
print("ALL OK" if all_ok else "SOME MISMATCHES FOUND")
