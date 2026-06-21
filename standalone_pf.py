"""
Standalone Newton-Raphson Power Flow for IEEE 14-bus mosaik network.
Uses exact same network parameters as scenario_adaptive.py.
Identifies slack buses same way as the scenario.
"""
import os, sys, cmath, math
import numpy as np
from dotenv import load_dotenv
from neo4j import GraphDatabase

load_dotenv('.env')

# ── Fetch network data using same queries as scenario_adaptive.py ─────────────
driver = GraphDatabase.driver(os.getenv('NEO4J_URI'),
    auth=(os.getenv('NEO4J_USERNAME'), os.getenv('NEO4J_PASSWORD')))
db = os.getenv('NEO4J_DATABASE')

with driver.session(database=db) as sess:
    sub_records = sess.run(
        'MATCH (s:Substation) RETURN s.name AS name, s.nominal_voltage_kv AS v_nom, '
        's.is_slack AS is_slack, s.is_sync_machine AS is_sync_machine, '
        's.sv_voltage_kv AS sv_v, s.sv_angle_deg AS sv_ang, '
        's.p_gen_mw AS p_gen_mw, s.q_gen_mvar AS q_gen_mvar ORDER BY s.name').data()
    subs = {}
    for r in sub_records:
        name = r['name']
        subs[name] = {
            'v_nom': r['v_nom'] or 20.0,
            'is_slack': bool(r['is_slack']) if r['is_slack'] is not None else False,
            'is_sync': bool(r['is_sync_machine']) if r['is_sync_machine'] is not None else False,
            'sv_v': r['sv_v'] or r['v_nom'] or 20.0,
            'sv_ang': r['sv_ang'] or 0.0,
            'p_gen': r['p_gen_mw'] or 0.0,
            'q_gen': r['q_gen_mvar'] or 0.0,
        }

    line_records = sess.run(
        'MATCH (a:Substation)-[l:LINE]->(b:Substation) '
        'RETURN a.name AS from_sub, b.name AS to_sub, '
        'l.r_ohm AS r_ohm, l.x_ohm AS x_ohm, l.bch AS bch '
        'ORDER BY a.name, b.name').data()

    tr_records = sess.run('MATCH (t:Transformer) RETURN t ORDER BY t.name').data()
    transformers = {r['t']['name']: dict(r['t']) for r in tr_records}

    # Same query as scenario for LV-slack detection
    tr_edge_records = sess.run(
        'MATCH (hv:Substation)-[:CONNECT_TO {side:"HV"}]->(t:Transformer)'
        '-[:CONNECT_TO {side:"LV"}]->(lv:Substation) '
        'RETURN hv.name AS hv_sub, t.name AS tr_name, lv.name AS lv_sub').data()

    load_records = sess.run(
        'MATCH (s:Substation)-[:CONNECT_TO]->(l:Load) '
        'RETURN s.name AS sub_name, l.p_mw AS p_mw, l.q_mvar AS q_mvar').data()
    loads = {r['sub_name']: {'p_mw': r['p_mw'] or 0.0, 'q_mvar': r['q_mvar'] or 0.0}
             for r in load_records}

driver.close()

# ── Determine slack buses (same logic as scenario_adaptive.py) ─────────────────
slack_subs_primary = {name for name, s in subs.items() if s['is_slack']}
sync_machine_subs  = {name for name, s in subs.items() if s['is_sync']}

# LV transformer buses that are NOT sync machines (same as _lv_slack_subs)
lv_slack_subs = {e['lv_sub'] for e in tr_edge_records
                 if e['lv_sub'] not in slack_subs_primary
                 and not subs.get(e['lv_sub'], {}).get('is_sync', False)}

all_slack_subs = slack_subs_primary | lv_slack_subs

print(f"Primary slack buses: {sorted(slack_subs_primary)}")
print(f"LV slack buses:      {sorted(lv_slack_subs)}")
print(f"All slack buses:     {sorted(all_slack_subs)}")
print(f"Sync machine buses:  {sorted(sync_machine_subs)}")

# ── Build bus index ───────────────────────────────────────────────────────────
bus_names = sorted(subs.keys())
n = len(bus_names)
idx = {name: i for i, name in enumerate(bus_names)}

# ── Transformer helper (same as _tr_x_hv in scenario) ────────────────────────
def tr_hv_impedance(t):
    x_hv = t.get('hv_x_ohm') or 0.0
    x_lv = t.get('lv_x_ohm') or 0.0
    r_hv = t.get('hv_r_ohm') or 0.0
    r_lv = t.get('lv_r_ohm') or 0.0
    if abs(x_hv) < 1e-12 and abs(x_lv) > 1e-12:
        u1 = t.get('hv_rated_u_kv') or t.get('hv_nominal_voltage_kv') or 1.0
        u2 = t.get('lv_rated_u_kv') or t.get('lv_nominal_voltage_kv') or 1.0
        x_hv = x_lv * (u1/u2)**2
        r_hv = r_lv * (u1/u2)**2
    return complex(r_hv, x_hv)

# ── Build Y-bus ───────────────────────────────────────────────────────────────
Y = np.zeros((n, n), dtype=complex)

for l in line_records:
    fi, ti = idx[l['from_sub']], idx[l['to_sub']]
    Z = complex(l['r_ohm'], l['x_ohm'])
    y_s = 1.0/Z if abs(Z) > 1e-15 else 0j
    y_sh = complex(0, (l['bch'] or 0)/2)
    Y[fi][fi] += y_s + y_sh
    Y[ti][ti] += y_s + y_sh
    Y[fi][ti] -= y_s
    Y[ti][fi] -= y_s

tr_connections = []
for e in tr_edge_records:
    hv_name, lv_name, tr_name = e['hv_sub'], e['lv_sub'], e['tr_name']
    # Identify HV/LV by nominal voltage
    hv_nom = subs[hv_name]['v_nom']
    lv_nom = subs[lv_name]['v_nom']
    if hv_nom < lv_nom:
        hv_name, lv_name = lv_name, hv_name
    t = transformers.get(tr_name, {})
    Z = tr_hv_impedance(t)
    u1 = t.get('hv_rated_u_kv') or t.get('hv_nominal_voltage_kv') or subs[hv_name]['v_nom']
    u2 = t.get('lv_rated_u_kv') or t.get('lv_nominal_voltage_kv') or subs[lv_name]['v_nom']
    tap = u1 / u2  # off-nominal tap ratio
    y_s = 1.0/Z if abs(Z) > 1e-15 else 0j
    hi, li = idx[hv_name], idx[lv_name]
    Y[hi][hi] += y_s / tap**2
    Y[li][li] += y_s
    Y[hi][li] -= y_s / tap
    Y[li][hi] -= y_s / tap
    tr_connections.append({'hv': hv_name, 'lv': lv_name, 'Z': Z, 'tap': tap})
    print(f"  Transformer {tr_name}: HV={hv_name}({subs[hv_name]['v_nom']}kV), "
          f"LV={lv_name}({subs[lv_name]['v_nom']}kV), Z={Z:.4f}Ω, tap={tap:.4f}")

# ── Bus types and scheduled power ─────────────────────────────────────────────
# In simulation (new code): only Bus 2 has sync gen (P_gen=40 MW).
# Bus 3, 6, 8 have is_sync=True but P_gen=0 → skipped in new code.
# Loads are always present.
# PV buses: sync machine buses NOT in all_slack_subs
pv_buses = sorted([name for name in sync_machine_subs if name not in all_slack_subs])
pq_buses = sorted([name for name in bus_names
                   if name not in all_slack_subs and name not in pv_buses])

print(f"\nBus types:")
print(f"  Slack: {sorted(all_slack_subs)}")
print(f"  PV:    {pv_buses}")
print(f"  PQ:    {pq_buses}")

# Power injections: P_sch = P_gen - P_load (net injection, positive=generation)
P_sch = {}  # MW scheduled net injection
Q_sch = {}  # Mvar scheduled net injection (used only for PQ buses)
V_mag_spec = {}  # Specified voltage magnitude for PV and slack buses

for name, s in subs.items():
    p_load = loads.get(name, {}).get('p_mw', 0.0)
    q_load = loads.get(name, {}).get('q_mvar', 0.0)
    # In NEW simulation code: only Bus 2 has sync gen; Bus 3,6,8 have P_gen=0 → no gen entity
    p_gen = s['p_gen']  # Use full p_gen from Neo4j (0 for Bus 3,6,8)
    q_gen = s['q_gen']  # q_gen irrelevant for PV buses (Q is free)
    P_sch[name] = p_gen - p_load
    Q_sch[name] = q_gen - q_load
    V_mag_spec[name] = s['sv_v']  # CIM voltage magnitude used as V_reg for PV buses

print(f"\nScheduled injections (P_gen - P_load):")
for name in bus_names:
    print(f"  {name}: P_sch={P_sch[name]:.2f} MW, Q_sch={Q_sch[name]:.2f} Mvar, "
          f"|V|_spec={V_mag_spec[name]:.3f} kV, is_slack={name in all_slack_subs}, "
          f"is_pv={name in pv_buses}")

# ── NR Power Flow ──────────────────────────────────────────────────────────────
# Initialize from CIM values
V = {}
for name, s in subs.items():
    V[name] = cmath.rect(s['sv_v'], math.radians(s['sv_ang']))

non_slack = [name for name in bus_names if name not in all_slack_subs]
pv_set = set(pv_buses)
pq_set = set(pq_buses)

def calc_PQ(V):
    """Calculate P and Q injection at each bus from Y-bus."""
    P_calc, Q_calc = {}, {}
    for name in bus_names:
        i = idx[name]
        I_i = sum(Y[i][j] * V[bus_names[j]] for j in range(n))
        S_i = V[name] * I_i.conjugate()
        P_calc[name] = S_i.real
        Q_calc[name] = S_i.imag
    return P_calc, Q_calc

print("\n=== Running NR Power Flow ===")
for nrit in range(100):
    P_calc, Q_calc = calc_PQ(V)

    # Mismatch
    dP = {name: P_sch[name] - P_calc[name] for name in non_slack}
    dQ = {name: Q_sch[name] - Q_calc[name] for name in pq_buses}

    f = np.array([dP[n] for n in non_slack] + [dQ[n] for n in pq_buses])
    err = np.max(np.abs(f))
    if err < 1e-6:
        print(f"Converged at iteration {nrit+1}, max mismatch = {err:.2e} MW/Mvar")
        break
    if nrit % 5 == 0:
        print(f"  Iter {nrit}: max mismatch = {err:.3f}")

    # Build Jacobian (polar form)
    nP = len(non_slack)
    nQ = len(pq_buses)
    J = np.zeros((nP + nQ, nP + nQ))

    # H block: dP/dθ  (rows=non_slack, cols=non_slack)
    for ri, r_name in enumerate(non_slack):
        ir = idx[r_name]
        Vr = V[r_name]; Vr_mag = abs(Vr); θr = cmath.phase(Vr)
        for ci, c_name in enumerate(non_slack):
            ic = idx[c_name]
            Vc = V[c_name]; Vc_mag = abs(Vc); θc = cmath.phase(Vc)
            G = Y[ir][ic].real; B = Y[ir][ic].imag
            dθ = θr - θc
            if ri == ci:
                J[ri][ci] = -Q_calc[r_name] - B * Vr_mag**2
            else:
                J[ri][ci] = -Vr_mag * Vc_mag * (G * math.sin(dθ) - B * math.cos(dθ))

    # N block: dP/d|V|  (rows=non_slack, cols=pq_buses)
    for ri, r_name in enumerate(non_slack):
        ir = idx[r_name]
        Vr = V[r_name]; Vr_mag = abs(Vr); θr = cmath.phase(Vr)
        for ci, c_name in enumerate(pq_buses):
            ic = idx[c_name]
            Vc = V[c_name]; Vc_mag = abs(Vc); θc = cmath.phase(Vc)
            G = Y[ir][ic].real; B = Y[ir][ic].imag
            dθ = θr - θc
            if r_name == c_name:
                J[ri][nP + ci] = P_calc[r_name] / Vr_mag + G * Vr_mag
            else:
                J[ri][nP + ci] = Vr_mag * (G * math.cos(dθ) + B * math.sin(dθ))

    # J block: dQ/dθ  (rows=pq_buses, cols=non_slack)
    for ri, r_name in enumerate(pq_buses):
        ir = idx[r_name]
        Vr = V[r_name]; Vr_mag = abs(Vr); θr = cmath.phase(Vr)
        for ci, c_name in enumerate(non_slack):
            ic = idx[c_name]
            Vc = V[c_name]; Vc_mag = abs(Vc); θc = cmath.phase(Vc)
            G = Y[ir][ic].real; B = Y[ir][ic].imag
            dθ = θr - θc
            if r_name == c_name:
                J[nP + ri][ci] = P_calc[r_name] - G * Vr_mag**2
            else:
                J[nP + ri][ci] = Vr_mag * Vc_mag * (G * math.cos(dθ) + B * math.sin(dθ))

    # L block: dQ/d|V|  (rows=pq_buses, cols=pq_buses)
    for ri, r_name in enumerate(pq_buses):
        ir = idx[r_name]
        Vr = V[r_name]; Vr_mag = abs(Vr); θr = cmath.phase(Vr)
        for ci, c_name in enumerate(pq_buses):
            ic = idx[c_name]
            Vc = V[c_name]; Vc_mag = abs(Vc); θc = cmath.phase(Vc)
            G = Y[ir][ic].real; B = Y[ir][ic].imag
            dθ = θr - θc
            if r_name == c_name:
                J[nP + ri][nP + ci] = Q_calc[r_name] / Vr_mag - B * Vr_mag
            else:
                J[nP + ri][nP + ci] = Vr_mag * (G * math.sin(dθ) - B * math.cos(dθ))

    # Solve J * [Δθ; Δ|V|/|V|] = f (Newton-Raphson with normalized voltage correction)
    # Use Δ|V| directly (not Δ|V|/|V|) to match N/L blocks above
    try:
        dx = np.linalg.solve(J, f)
    except np.linalg.LinAlgError:
        print("Singular Jacobian! Stopping."); break

    # Update voltage angles for non-slack buses
    for ci, c_name in enumerate(non_slack):
        dtheta = dx[ci]
        V[c_name] = cmath.rect(abs(V[c_name]), cmath.phase(V[c_name]) + dtheta)

    # Update voltage magnitudes for PQ buses
    for ci, c_name in enumerate(pq_buses):
        dV = dx[nP + ci]
        V[c_name] = cmath.rect(abs(V[c_name]) + dV, cmath.phase(V[c_name]))

    # For PV buses: magnitude is FIXED (don't update magnitude)
    # (Already excluded from pq_buses, so their magnitude stays at init)
    # But we DID update angle via the Δθ step above → OK

print("\n=== Results: NR Power Flow vs CIM SvVoltage vs Simulation ===")
sim = {
    'BUS_1   69.0': (73.14, 0.0),
    'BUS_2   69.0': (72.105, -3.4371),
    'BUS_3   69.0': (69.690, -10.0551),
    'BUS_4   69.0': (70.869, -6.622),
    'BUS_5   69.0': (71.338, -5.399),
    'BUS_6   13.8': (14.766, -20.063),
    'BUS_7   13.8': (14.460, -13.225),
    'BUS_8   18.0': (19.561, -13.225),
    'BUS_9   13.8': (14.238, -14.809),
    'BUS_10  13.8': (14.231, -16.030),
    'BUS_11  13.8': (14.449, -18.144),
    'BUS_12  13.8': (14.520, -20.535),
    'BUS_13  13.8': (14.451, -20.167),
    'BUS_14  13.8': (14.068, -18.187),
}
V_CIM = {name: cmath.rect(s['sv_v'], math.radians(s['sv_ang'])) for name, s in subs.items()}

print(f"\n{'Bus':20} {'|V|_NR':8} {'θ_NR':8}  {'|V|_CIM':8} {'θ_CIM':8}  {'|V|_sim':8} {'θ_sim':8}  {'ΔP_calc':9}")
P_calc, Q_calc = calc_PQ(V)
for name in bus_names:
    v_nr = abs(V[name]); a_nr = math.degrees(cmath.phase(V[name]))
    v_cim = abs(V_CIM[name]); a_cim = math.degrees(cmath.phase(V_CIM[name]))
    v_s, a_s = sim.get(name, (0, 0))
    dp = P_sch[name] - P_calc[name]
    marker = ' *** CIM' if abs(v_nr - v_cim) < 0.01 and abs(a_nr - a_cim) < 0.1 else ''
    marker = ' === SIM' if abs(v_nr - v_s) < 0.01 and abs(a_nr - a_s) < 0.1 and not marker else marker
    print(f"{name:20} {v_nr:8.3f} {a_nr:8.3f}  {v_cim:8.3f} {a_cim:8.3f}  {v_s:8.3f} {a_s:8.3f}  {dp:9.4f}{marker}")

print(f"\nQ injections at PV/slack buses (reactive power absorbed):")
for name in pv_buses + sorted(all_slack_subs):
    print(f"  {name}: Q_calc={Q_calc[name]:.3f} Mvar, Q_sch={Q_sch[name]:.3f} Mvar (Neo4j)")

# Write a minimal JSON snapshot compatible with the Collector output naming so
# `tools/compare_nr_results.py` can compare mosaik output and the monolithic NR.
try:
    import json
    SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, os.path.join(SCRIPT_DIR, 'src'))
    from connect import _sub_var

    out = {}
    for name in bus_names:
        sv = _sub_var(name)
        mag_key = f'V_{sv}_mag_kv'
        ang_key = f'V_{sv}_ang_deg'
        out[mag_key] = {'0': abs(V[name])}
        out[ang_key] = {'0': math.degrees(cmath.phase(V[name]))}

    out_path = os.path.join(SCRIPT_DIR, 'output_NR.json')
    with open(out_path, 'w') as f:
        json.dump(out, f, indent=2)
    print(f'Wrote {out_path}')
except Exception as e:
    print(f'Could not write output_NR.json: {e}')
