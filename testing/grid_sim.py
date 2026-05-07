"""
Mosaik GridSim — Backward/Forward Sweep and Latency Insertion Method (LIM)
AC power flow for the CIGRE MV grid.

No external solver. Network topology and line parameters are loaded directly
from Neo4j (the same graph built by add_data.py).

Graph assumptions:
  (Ni:Substation)-[:LINE {r_ohm, x_ohm, bch, name}]->(Nj:Substation)
  Slack bus: N1  (20 kV, LV side of the HV/MV transformer)
  All voltages in kV, powers in MW/MVAr, impedances in Ω.

Two solver methods are available (select via GridSim init param 'method'):
  'bfs'  — Backward/Forward Sweep   (fast, radial-only)
  'lim'  — Latency Insertion Method  (explicit, naturally decoupled)

LIM in co-simulation
---------------------
LIM inserts a numerical inductance L_num in each branch and a numerical
capacitance C_num at each bus.  The explicit update rules are:

  I_ij^(k+1) = I_ij^(k) + (dt/L_num) * (V_i^(k) - V_j^(k) - Z_ij * I_ij^(k))
  V_i^(k+1)  = V_i^(k)  + (dt/C_num) * (ΣI_in - ΣI_out - I_load - jB_i*V_i)

Fixed point ↔ KVL/KCL: at convergence, Z_ij*I_ij = ΔV  and  ΣI = I_load + jBV.

For Mosaik co-simulation the one-step delay between GridSim and external
simulators (battery, load) IS the LIM latency.  The stable Mosaik step size
for a controller with droop gain k_d is:

  Δt_mosaik  <  2 * Z_bus / k_d

where Z_bus is the Thevenin impedance seen from the battery bus.
"""

import cmath
import math
import os

import mosaik_api_v3
from dotenv import load_dotenv
from neo4j import GraphDatabase

load_dotenv()

NEO4J_URI      = os.getenv("NEO4J_URI")
NEO4J_USERNAME = os.getenv("NEO4J_USERNAME")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD")
NEO4J_DATABASE = os.getenv("NEO4J_DATABASE")

SLACK_BUS     = "N1"
SLACK_VOLTAGE = 20.0   # kV (nominal LV side)
MAX_ITER      = 20
TOLERANCE     = 1e-6   # kV
OMEGA         = 2 * math.pi * 50  # rad/s  (50 Hz grid)

META = {
    "type": "time-based",
    "models": {
        "Grid": {
            "public": True,
            "any_inputs": True,
            "params": ["method"],
            "attrs": ["V_kv", "V_ang_deg", "P_mw", "Q_mvar",
                       "lim_dt_s", "lim_iterations"],
        }
    },
}


# ── Neo4j helpers ─────────────────────────────────────────────────────────────

def load_network_from_neo4j():
    """
    Return:
      buses     : set of bus names
      lines     : list of {name, from, to, r, x, bch}
      nominal   : dict name -> nominal_voltage_kv
      slack_buses: dict bus_name -> slack_voltage_kv  (one per transformer LV side)
    """
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USERNAME, NEO4J_PASSWORD))
    buses, lines, nominal, slack_buses = set(), [], {}, {}

    with driver.session(database=NEO4J_DATABASE) as session:
        for rec in session.run("MATCH (n:Substation) RETURN n.name AS name, n.nominal_voltage_kv AS vnom"):
            buses.add(rec["name"])
            if rec["vnom"] is not None:
                nominal[rec["name"]] = rec["vnom"]

        for rec in session.run(
            """
            MATCH (a:Substation)-[r:LINE]->(b:Substation)
            RETURN a.name AS frm, b.name AS to,
                   r.name AS name,
                   r.r_ohm AS r, r.x_ohm AS x, r.bch AS bch
            """
        ):
            lines.append({
                "name": rec["name"],
                "from": rec["frm"],
                "to":   rec["to"],
                "r":    rec["r"]   or 0.0,
                "x":    rec["x"]   or 0.0,
                "bch":  rec["bch"] or 0.0,
            })

        # Transformer LV buses are slack buses; use their nominal voltage
        for rec in session.run(
            """
            MATCH (t:Transformer)-[:CONNECT_TO {side: 'LV'}]->(s:Substation)
            RETURN s.name AS bus, t.lv_rated_u_kv AS v_kv
            """
        ):
            v = rec["v_kv"] or nominal.get(rec["bus"], 20.0)
            slack_buses[rec["bus"]] = float(v)

    driver.close()
    # Fallback: if no transformer data found, use hardcoded default
    if not slack_buses:
        slack_buses = {SLACK_BUS: SLACK_VOLTAGE}
    return buses, lines, nominal, slack_buses


# ── Latency Insertion Method ──────────────────────────────────────────────────

def lim_stability_dt(lines, bus_C_num):
    """
    Compute the maximum stable LIM time step across all branches.

    Stability requires  dt < 2 * sqrt(L_num * C_num_min_endpoint)  AND
                        dt < 2 * L_num / |Z|  for each branch.
    Returns (dt_stable, branch_limits) where branch_limits is a dict for
    inspection.
    """
    limits = {}
    for ln in lines:
        k = (ln["from"], ln["to"])
        x = ln["x"] if ln["x"] > 0 else ln["r"]
        L_num = x / OMEGA
        Z_mag = abs(complex(ln["r"], ln["x"]))
        C_min = min(bus_C_num.get(ln["from"], 1e-10),
                    bus_C_num.get(ln["to"],   1e-10))
        dt_lc = 2 * math.sqrt(L_num * C_min)          # LC stability
        dt_rl = 2 * L_num / Z_mag if Z_mag > 0 else float("inf")  # RL stability
        limits[k] = min(dt_lc, dt_rl)
    dt = 0.9 * min(limits.values())  # 10 % safety margin
    return dt, limits


def lim_sweep(buses, lines, slack, slack_v, loads, max_iter=5000, tol=1e-6,
              omega=0.5):
    """
    Phasor-domain LIM power flow solver.

    BRANCH update  (explicit Ohm's law, L_num → 0):
      I_ij^(k+1) = (V_i^(k) - V_j^(k)) / Z_ij

    BUS update  (under-relaxed Jacobi, C_num = 1/(omega * Y_self)):
      residual   = ΣI_in^(k+1) - ΣI_out^(k+1) - I_load(V^(k)) - jB·V^(k)
      V_i^(k+1) = V_i^(k) + omega * residual / Y_self_i

    Why under-relaxation is REQUIRED for radial networks
    -----------------------------------------------------
    For a tree topology, shunt susceptance (bch) is negligible, so
    Y_off ≈ |Y_self| at every bus → Jacobi spectral radius ρ ≥ 1.
    Bare Jacobi (omega=1) diverges. Under-relaxation with omega < 1
    reduces effective ρ to ≈ (1 − omega) < 1.
    Default omega=0.5 gives ρ_eff ≈ 0.5 → converges in ~30 iterations.

    LIM interpretation
    ------------------
    * Branch: L_num → 0 (instantaneous)
    * Bus:    C_num = 1/(omega·Y_self) — latency scaled for convergence

    Mosaik external latency (the real LIM story for co-simulation)
    ---------------------------------------------------------------
    External simulators (battery, load) provide P/Q from the PREVIOUS
    Mosaik step.  That one-step delay IS the LIM latency at the
    co-simulation interface.  Safe Mosaik step size for a droop
    controller with gain k_d:
      Δt_mosaik  <  2 |Z_Thevenin(bus)| / k_d

    The physical dt (from L and C of the lines) is returned as dt_emtp;
    it gives the absolute lower bound for EMTP-level battery controllers.

    Returns
    -------
    V          : dict  bus -> complex voltage (kV)
    S_branch   : dict  (from, to) -> complex power at sending end (MVA)
    iterations : int
    dt_emtp    : float  physical LIM stability step (s) — Mosaik step hint
    """
    # ── Per-branch parameters ────────────────────────────────────────────────
    bp        = {}                         # (from, to) -> {Z, from, to}
    bus_B     = {b: 0.0   for b in buses}  # shunt susceptance (S)
    bus_Yself = {b: 0+0j  for b in buses}  # sum of branch admittances at bus

    for ln in lines:
        k  = (ln["from"], ln["to"])
        Z  = complex(ln["r"], ln["x"])
        y  = 1.0 / Z
        bp[k] = {"Z": Z, "y": y, "from": ln["from"], "to": ln["to"],
                 "L_num": (ln["x"] if ln["x"] > 0 else ln["r"]) / OMEGA}
        bus_B[ln["from"]]     += ln["bch"] / 2
        bus_B[ln["to"]]       += ln["bch"] / 2
        bus_Yself[ln["from"]] += y
        bus_Yself[ln["to"]]   += y

    # Include shunt susceptance in self-admittance
    for b in buses:
        bus_Yself[b] += complex(0, bus_B[b])

    # ── Physical EMTP dt (for Mosaik step size guidance) ─────────────────────
    bus_C_phys = {b: 0.0 for b in buses}
    for ln in lines:
        c = ln["bch"] / (2 * OMEGA)
        bus_C_phys[ln["from"]] += c
        bus_C_phys[ln["to"]]   += c
    for b in buses:
        if bus_C_phys[b] < 1e-12:
            r_adj = [ln["r"] for ln in lines
                     if (ln["from"] == b or ln["to"] == b) and ln["r"] > 0]
            G = sum(1 / r for r in r_adj) if r_adj else 1.0
            bus_C_phys[b] = 1.0 / (OMEGA * G)
    dt_emtp, _ = lim_stability_dt(lines, bus_C_phys)

    # ── Iterate ───────────────────────────────────────────────────────────────
    V = {b: slack_v for b in buses}
    I = {k: complex(0) for k in bp}

    iters = 0
    for iters in range(1, max_iter + 1):
        V_prev = dict(V)

        # Step 1 — branch currents: explicit Ohm's law
        for k, b in bp.items():
            I[k] = (V[b["from"]] - V[b["to"]]) / b["Z"]

        # Step 2 — bus voltages: Jacobi step scaled by 1/Y_self
        for b in buses:
            if b == slack:
                V[b] = slack_v
                continue
            I_net = (sum(I[k] for k, par in bp.items() if par["to"]   == b)
                   - sum(I[k] for k, par in bp.items() if par["from"] == b))
            S_load  = loads.get(b, 0+0j)
            I_load  = (S_load / V_prev[b]).conjugate() if abs(V_prev[b]) > 0 else 0+0j
            I_shunt = complex(0, bus_B[b]) * V_prev[b]
            residual = I_net - I_load - I_shunt
            # under-relaxed Jacobi: omega < 1 required to converge on trees
            V[b] = V_prev[b] + omega * residual / bus_Yself[b]

        if max(abs(V[b] - V_prev[b]) for b in buses if b != slack) < tol:
            break

    S_branch = {k: V[par["from"]] * I[k].conjugate() for k, par in bp.items()}
    return V, S_branch, iters, dt_emtp


def solve_all_feeders_lim(buses, lines, slack_buses, loads):
    """Like solve_all_feeders but using LIM instead of B/F sweep."""
    components = _connected_components(buses, lines)
    V_all, S_all = {}, {}
    total_iters, total_dt = 0, float("inf")

    for comp in components:
        slacks_in_comp = {b: v for b, v in slack_buses.items() if b in comp}
        if not slacks_in_comp:
            for b in comp:
                V_all[b] = complex(0, 0)
            continue
        slack, slack_v_kv = next(iter(slacks_in_comp.items()))
        comp_lines = [ln for ln in lines if ln["from"] in comp and ln["to"] in comp]
        comp_loads = {b: loads.get(b, 0+0j) for b in comp}
        V, S, iters, dt = lim_sweep(comp, comp_lines, slack,
                                     complex(slack_v_kv, 0), comp_loads)
        V_all.update(V)
        S_all.update(S)
        total_iters += iters
        total_dt = min(total_dt, dt)

    return V_all, S_all, total_iters, total_dt


# ── Backward/Forward Sweep ────────────────────────────────────────────────────

def _build_tree(buses, lines, slack):
    """
    Return parent map and ordered traversal lists for the radial tree.
      children[bus]  = list of (child_bus, line_dict)
      bfs_order      = buses in BFS order from slack (root first)
    """
    # Build adjacency (undirected)
    adj = {b: [] for b in buses}
    for ln in lines:
        adj[ln["from"]].append((ln["to"],   ln))
        adj[ln["to"]].append((ln["from"], ln))

    parent   = {}           # bus -> (parent_bus, line_dict)
    children = {b: [] for b in buses}
    visited  = {slack}
    queue    = [slack]
    bfs_order = [slack]

    while queue:
        node = queue.pop(0)
        for neighbour, ln in adj[node]:
            if neighbour not in visited:
                visited.add(neighbour)
                parent[neighbour]   = (node, ln)
                children[node].append((neighbour, ln))
                queue.append(neighbour)
                bfs_order.append(neighbour)

    return parent, children, bfs_order


def _connected_components(buses, lines):
    """Return list of sets, each set being one connected component via LINE edges."""
    adj = {b: set() for b in buses}
    for ln in lines:
        adj[ln["from"]].add(ln["to"])
        adj[ln["to"]].add(ln["from"])

    visited, components = set(), []
    for start in buses:
        if start in visited:
            continue
        comp, queue = set(), [start]
        while queue:
            node = queue.pop()
            if node in visited:
                continue
            visited.add(node)
            comp.add(node)
            queue.extend(adj[node] - visited)
        components.append(comp)
    return components


def solve_all_feeders(buses, lines, slack_buses, loads):
    """
    Detect connected components (feeders), find the slack bus in each,
    and run B/F sweep on each independently.

    Returns merged V and S_branch dicts.
    """
    components = _connected_components(buses, lines)
    V_all, S_all = {}, {}

    for comp in components:
        # Find which slack bus belongs to this component
        slacks_in_comp = {b: v for b, v in slack_buses.items() if b in comp}
        if not slacks_in_comp:
            # No transformer LV bus found — skip (e.g. N0 is isolated via transformer)
            for b in comp:
                V_all[b] = complex(0, 0)
            continue

        slack, slack_v_kv = next(iter(slacks_in_comp.items()))
        comp_lines = [ln for ln in lines if ln["from"] in comp and ln["to"] in comp]
        comp_loads = {b: loads.get(b, 0+0j) for b in comp}

        V, S = backward_forward_sweep(comp, comp_lines, slack,
                                      complex(slack_v_kv, 0), comp_loads)
        V_all.update(V)
        S_all.update(S)

    return V_all, S_all


def backward_forward_sweep(buses, lines, slack, slack_v_kv, loads):
    """
    Solve the radial AC power flow using the B/F sweep method.

    Parameters
    ----------
    buses      : set of bus names
    lines      : list of line dicts  {from, to, r, x, bch}
    slack      : name of the slack bus (fixed voltage)
    slack_v_kv : complex voltage at slack bus (kV)
    loads      : dict  bus_name -> complex S (MW + j MVAr)

    Returns
    -------
    V   : dict  bus_name -> complex voltage (kV)
    S_branch : dict  (from, to) -> complex S flow (MVA) at sending end
    """
    parent, children, bfs_order = _build_tree(buses, lines, slack)

    # Initialise all voltages to slack
    V = {b: slack_v_kv for b in buses}

    for _ in range(MAX_ITER):
        V_prev = dict(V)

        # ── Backward pass: compute branch currents leaf → root ────────────────
        I_branch = {}   # (par_bus, bus) -> current flowing from par_bus into bus

        for bus in reversed(bfs_order[1:]):   # leaves first, skip slack
            par_bus, ln = parent[bus]
            jb2 = complex(0, ln["bch"] / 2)

            # Load current drawn from this bus
            S_load = loads.get(bus, 0+0j)
            I_load = (S_load / V[bus]).conjugate() if abs(V[bus]) > 0 else 0+0j

            # Shunt capacitor at this bus injects reactive current → reduces branch demand
            I_shunt_inj = jb2 * V[bus]

            # Sum of currents already flowing from this bus into each child branch
            I_children_out = sum(
                I_branch.get((bus, child), 0+0j)
                for child, _ in children[bus]
            )

            # Current the parent must supply through the branch
            I_branch[(par_bus, bus)] = I_load - I_shunt_inj + I_children_out

        # ── Forward pass: update voltages root → leaves ───────────────────────
        V[slack] = slack_v_kv

        for bus in bfs_order[1:]:
            par_bus, ln = parent[bus]
            z = complex(ln["r"], ln["x"])
            I = I_branch.get((par_bus, bus), 0+0j)
            V[bus] = V[par_bus] - z * I

        # ── Convergence check ─────────────────────────────────────────────────
        if all(abs(V[b] - V_prev[b]) < TOLERANCE for b in buses):
            break

    # Branch power flows at sending end
    S_branch = {}
    for bus in bfs_order[1:]:
        par_bus, ln = parent[bus]
        I = I_branch.get((par_bus, bus), 0+0j)
        S_branch[(par_bus, bus)] = V[par_bus] * I.conjugate()

    return V, S_branch


# ── Mosaik Simulator ──────────────────────────────────────────────────────────

class GridSim(mosaik_api_v3.Simulator):

    def __init__(self):
        super().__init__(META)
        self.buses       = None
        self.lines       = None
        self.nominal     = None
        self.slack_buses = {}   # bus_name -> slack_voltage_kv
        self.loads       = {}   # bus_name -> complex S (MW + jMVAr)
        self.results     = {}   # bus_name -> {V_kv, V_ang_deg, P_mw, Q_mvar}
        self.eid_map     = {}   # entity_id -> bus_name
        self.step_size   = 1
        self.method      = "bfs"  # 'bfs' or 'lim'
        self.lim_dt      = None   # stable LIM step (seconds)
        self.lim_iters   = 0

    def init(self, sid, time_resolution=1.0, step_size=1, method="bfs", **kwargs):
        self.step_size = step_size
        self.method    = method
        print("[GridSim] Loading network from Neo4j …")
        self.buses, self.lines, self.nominal, self.slack_buses = load_network_from_neo4j()
        print(f"[GridSim]   {len(self.buses)} buses, {len(self.lines)} lines, "
              f"{len(self.slack_buses)} slack bus(es): {list(self.slack_buses)}")
        print(f"[GridSim]   solver method = {self.method}")

        self.loads = {b: complex(0, 0) for b in self.buses}
        return self.meta

    def create(self, num, model, **kwargs):
        entities = []
        for bus in sorted(self.buses):
            eid = f"Bus_{bus}"
            self.eid_map[eid] = bus
            entities.append({"eid": eid, "type": model})
        return entities

    def step(self, time, inputs, max_advance):
        # Apply P/Q injections from connected simulators
        for eid, attrs in inputs.items():
            bus = self.eid_map[eid]
            # Positive = generation/injection, negative = load
            p = sum(v for v in attrs.get("P_mw",   {}).values())
            q = sum(v for v in attrs.get("Q_mvar",  {}).values())
            # Loads are demands: positive input means positive load
            self.loads[bus] = complex(p, q)

        if self.method == "lim":
            V, S_branch, self.lim_iters, self.lim_dt = solve_all_feeders_lim(
                self.buses, self.lines, self.slack_buses, self.loads
            )
        else:
            V, S_branch = solve_all_feeders(
                self.buses, self.lines, self.slack_buses, self.loads
            )
            self.lim_iters, self.lim_dt = 0, None

        self.results = {}
        for bus, v in V.items():
            self.results[bus] = {
                "V_kv":           abs(v),
                "V_ang_deg":      math.degrees(cmath.phase(v)),
                "P_mw":           self.loads[bus].real,
                "Q_mvar":         self.loads[bus].imag,
                "lim_iterations": self.lim_iters,
                "lim_dt_s":       self.lim_dt,
            }

        return time + self.step_size

    def get_data(self, outputs):
        data = {}
        for eid, attrs in outputs.items():
            bus = self.eid_map[eid]
            data[eid] = {attr: self.results[bus][attr] for attr in attrs
                         if attr in self.results[bus]}
        return data


if __name__ == "__main__":
    mosaik_api_v3.start_simulation(GridSim())
