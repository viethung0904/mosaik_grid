"""
Simple grid simulation: 110 kV slack → Transformer (110/20 kV) → 2 substations
Each substation has 2 loads.

Topology
--------
  HV_Slack (110 kV, 0°)
       │
  Transformer (110/20 kV, Yd5, 40 MVA)
       │
  MV_Slack ──── Line_A ──── Sub_A  ← Load_A1 + Load_A2
           └─── Line_B ──── Sub_B  ← Load_B1 + Load_B2

All computation is plain Python — no FMU or simulation framework required.

Units throughout: kV, MW, MVAr, kA  (Z in Ω — consistent because Ω·kA = kV)
Transformer internals stay in SI (V, W, VAr) to match CGMES parameters.
"""

import cmath
import math
import matplotlib.pyplot as plt

# ─────────────────────────────────────────────────────────────────────────────
# 1.  Network data  — edit these to match your actual grid
# ─────────────────────────────────────────────────────────────────────────────

# Transformer (Yd5, 40 MVA, 110/20 kV) — CIGRE MV reference values
TR = dict(
    ratedU1 = 110e3,     # V   HV rated voltage
    ratedU2 = 20e3,      # V   LV rated voltage
    R       = 13.608,    # Ω   series resistance (HV side referred)
    X       = 68.04,     # Ω   series reactance  (HV side referred)
    B       = 3.375e-6,  # S   magnetising susceptance
    G       = 0.0,       # S   iron-loss conductance (not in CGMES XML)
)

HV_SLACK_KV  = 110.0   # kV — fixed HV busbar voltage
HV_SLACK_DEG = 0.0     # deg

# MV line impedances [Ω] and shunt susceptance [S]
LINE_A = dict(r=1.633, x=1.035, bch=0.0)   # MV_Slack → Sub_A
LINE_B = dict(r=2.190, x=1.380, bch=0.0)   # MV_Slack → Sub_B

# Individual loads at each substation [MW, MVAr]
LOAD_A1 = (0.500, 0.250)
LOAD_A2 = (0.300, 0.150)
LOAD_B1 = (0.432, 0.108)
LOAD_B2 = (0.275, 0.100)

OMEGA     = 0.5   # LIM under-relaxation factor (0.5 required for radial/tree networks)
MAX_LIM   = 5000  # max LIM Jacobi iterations
MAX_OUTER = 20    # max outer (transformer ↔ network) iterations
TOL       = 1e-6  # convergence threshold [kV]


# ─────────────────────────────────────────────────────────────────────────────
# 2.  Aggregate loads per substation  [MVA]
# ─────────────────────────────────────────────────────────────────────────────

S_A = complex(LOAD_A1[0] + LOAD_A2[0], LOAD_A1[1] + LOAD_A2[1])
S_B = complex(LOAD_B1[0] + LOAD_B2[0], LOAD_B1[1] + LOAD_B2[1])


# ─────────────────────────────────────────────────────────────────────────────
# 3.  Transformer — compute LV voltage magnitude [kV]
#
#  Γ-model (all series elements referred to HV side):
#    I        = (P2 − jQ2) / (√3 · conj(V1_ll))   [A]
#    V2_phase = V1_phase − Z·I
#    |V2_ll|  = √3 · |V2_phase| / a                [V]
# ─────────────────────────────────────────────────────────────────────────────

def transformer_v2(V1_kv, V1_deg, P2_mw, Q2_mvar):
    """Return LV voltage magnitude [kV] given HV voltage and LV apparent power."""
    V1_mag = V1_kv * 1e3          # kV → V
    P2     = P2_mw   * 1e6        # MW → W
    Q2     = Q2_mvar * 1e6        # MVAr → VAr

    if V1_mag <= 0.0:
        return 0.0

    sqrt3  = math.sqrt(3.0)
    V1_ll  = cmath.rect(V1_mag, math.radians(V1_deg))   # complex line-to-line phasor

    I      = complex(P2, -Q2) / (sqrt3 * V1_ll.conjugate())  # A
    Z      = complex(TR["R"], TR["X"])
    V2_ph  = (V1_ll / sqrt3) - Z * I                         # V (phase)

    a      = TR["ratedU1"] / TR["ratedU2"]    # turns ratio
    return sqrt3 * abs(V2_ph) / a / 1e3       # V → kV


# ─────────────────────────────────────────────────────────────────────────────
# 4.  MV network — LIM (Latency Insertion Method) Jacobi iteration
#
#  The LIM decomposes the network into branch and bus updates that use only
#  values from the PREVIOUS iteration — mirroring the one-step FMI coupling
#  delay that a real co-simulation master would impose.
#
#  Branch update (explicit Ohm's law, zero-inductance limit):
#    I_A^(k+1) = (V_slack^(k) − V_A^(k)) / Z_A
#    I_B^(k+1) = (V_slack^(k) − V_B^(k)) / Z_B
#
#  Bus update (under-relaxed Jacobi KCL):
#    I_load    = conj(S_load / V^(k))           constant-power load current
#    residual  = I_in^(k+1) − I_load            KCL error at this bus
#    V^(k+1)   = V^(k) + ω · residual / Y_self
#
#  Why ω < 1: for a radial/tree network Y_off ≈ Y_self at every bus,
#  so pure Jacobi (ω=1) has spectral radius ρ ≥ 1 and diverges.
#  ω = 0.5 gives ρ_eff ≈ 0.5, converging in ~3500 iterations.
# ─────────────────────────────────────────────────────────────────────────────

def solve_mv_network(V_slack_kv, record_history=False):
    """
    LIM Jacobi iteration for the 2-branch MV network.

    Returns
    -------
    V_A, V_B           : complex bus voltages [kV]
    S_from_A, S_from_B : complex sending-end apparent power [MVA]
    iters              : LIM iterations taken
    history            : dict of lists {bus: [|V| per iteration]} if record_history
    """
    V_slack = complex(V_slack_kv, 0.0)
    Z_A = complex(LINE_A["r"], LINE_A["x"])   # Ω  (Ω·kA = kV — no unit scaling)
    Z_B = complex(LINE_B["r"], LINE_B["x"])

    # Bus self-admittances: Y_self = Σ y_ij of all adjacent branches
    Y_self_A = 1.0 / Z_A   # only Line_A connects to Sub_A
    Y_self_B = 1.0 / Z_B   # only Line_B connects to Sub_B

    # Flat start at slack voltage
    V_A = V_slack
    V_B = V_slack

    history = {"MV_Slack": [], "Sub_A": [], "Sub_B": []} if record_history else None

    for iters in range(1, MAX_LIM + 1):
        # ── Step 1: branch currents from PREVIOUS voltages (explicit Ohm's law) ──
        I_A = (V_slack - V_A) / Z_A   # kA, from slack toward Sub_A
        I_B = (V_slack - V_B) / Z_B   # kA, from slack toward Sub_B

        # ── Step 2: constant-power load currents at each bus ──────────────────
        I_load_A = (S_A / V_A).conjugate() if abs(V_A) > 1e-9 else 0+0j
        I_load_B = (S_B / V_B).conjugate() if abs(V_B) > 1e-9 else 0+0j

        # ── Step 3: KCL residual and under-relaxed Jacobi bus update ──────────
        #  residual = net injected current − load current
        #  (no shunt term: bch = 0 for both lines)
        res_A = I_A - I_load_A
        res_B = I_B - I_load_B

        V_A_new = V_A + OMEGA * res_A / Y_self_A
        V_B_new = V_B + OMEGA * res_B / Y_self_B

        if record_history:
            history["MV_Slack"].append(V_slack_kv)
            history["Sub_A"].append(abs(V_A_new))
            history["Sub_B"].append(abs(V_B_new))

        if max(abs(V_A_new - V_A), abs(V_B_new - V_B)) < TOL:
            V_A, V_B = V_A_new, V_B_new
            break
        V_A, V_B = V_A_new, V_B_new

    # Sending-end apparent power [MVA] = V_slack · conj(I)
    I_A = (V_slack - V_A) / Z_A
    I_B = (V_slack - V_B) / Z_B
    S_from_A = V_slack * I_A.conjugate()
    S_from_B = V_slack * I_B.conjugate()

    return V_A, V_B, S_from_A, S_from_B, iters, history


# ─────────────────────────────────────────────────────────────────────────────
# 5.  Outer loop: converge transformer ↔ MV network
#
#  The transformer output voltage V2 depends on total MV apparent power (P2, Q2).
#  Total MV power (load + line losses) = sum of sending-end powers.
#  These depend on bus voltages, which depend on V2 (= V_mv_slack).
#  → iterate until V_mv_slack stops changing.
# ─────────────────────────────────────────────────────────────────────────────

V_mv_slack_kv = TR["ratedU2"] / 1e3   # initial guess: 20.0 kV (nominal)

print(f"{'Outer':>6}  {'V_mv kV':>10}  {'P2 MW':>8}  {'Q2 MVAr':>9}  {'LIM iters':>10}")

lim_history = None   # will hold voltage history from the final outer iteration

for outer in range(1, MAX_OUTER + 1):
    is_last = False   # re-evaluated after convergence check below
    V_A, V_B, S_from_A, S_from_B, bfs_iters, _ = solve_mv_network(V_mv_slack_kv)

    # Total apparent power drawn from transformer LV terminal [MVA]
    S_total = S_from_A + S_from_B
    P2_mw   = S_total.real
    Q2_mvar = S_total.imag

    V_mv_new_kv = transformer_v2(HV_SLACK_KV, HV_SLACK_DEG, P2_mw, Q2_mvar)

    print(f"{outer:>6}  {V_mv_new_kv:>10.6f}  {P2_mw:>8.4f}  {Q2_mvar:>9.4f}  {bfs_iters:>10}")

    if abs(V_mv_new_kv - V_mv_slack_kv) < TOL:
        V_mv_slack_kv = V_mv_new_kv
        # Re-run final iteration with history recording for the plot
        V_A, V_B, S_from_A, S_from_B, bfs_iters, lim_history = solve_mv_network(
            V_mv_slack_kv, record_history=True
        )
        break
    V_mv_slack_kv = V_mv_new_kv

# ─────────────────────────────────────────────────────────────────────────────
# 6.  Results
# ─────────────────────────────────────────────────────────────────────────────

print()
print("─" * 55)
print(f"  HV slack       : {HV_SLACK_KV:.3f} kV  ∠ {HV_SLACK_DEG:.2f}°")
print(f"  Transformer V2 : {V_mv_slack_kv:.4f} kV")
print(f"  Total P2       : {P2_mw:.4f} MW  (load + line losses)")
print(f"  Total Q2       : {Q2_mvar:.4f} MVAr")
print()
print(f"  {'Bus':<12}  {'|V| kV':>10}  {'angle °':>10}")
print(f"  {'-'*12}  {'-'*10}  {'-'*10}")
print(f"  {'MV_Slack':<12}  {V_mv_slack_kv:>10.4f}  {'0.0000':>10}")
print(f"  {'Sub_A':<12}  {abs(V_A):>10.4f}  {math.degrees(cmath.phase(V_A)):>10.4f}")
print(f"  {'Sub_B':<12}  {abs(V_B):>10.4f}  {math.degrees(cmath.phase(V_B)):>10.4f}")
print()
print("  Load breakdown:")
print(f"    Sub_A : Load_A1 = {LOAD_A1[0]} MW + {LOAD_A1[1]} MVAr")
print(f"            Load_A2 = {LOAD_A2[0]} MW + {LOAD_A2[1]} MVAr")
print(f"    Sub_B : Load_B1 = {LOAD_B1[0]} MW + {LOAD_B1[1]} MVAr")
print(f"            Load_B2 = {LOAD_B2[0]} MW + {LOAD_B2[1]} MVAr")

# ─────────────────────────────────────────────────────────────────────────────
# 7.  Voltage convergence plot
# ─────────────────────────────────────────────────────────────────────────────

if lim_history:
    iters_axis = range(1, len(lim_history["Sub_A"]) + 1)

    fig, axes = plt.subplots(2, 1, figsize=(10, 7), sharex=True)

    # ── Top: absolute voltage magnitude ──────────────────────────────────────
    ax1 = axes[0]
    ax1.plot(iters_axis, lim_history["MV_Slack"], color="tab:green",
             linewidth=1.5, label="MV_Slack (fixed)")
    ax1.plot(iters_axis, lim_history["Sub_A"],    color="tab:blue",
             linewidth=1.2, label="Sub_A")
    ax1.plot(iters_axis, lim_history["Sub_B"],    color="tab:orange",
             linewidth=1.2, label="Sub_B")
    ax1.set_ylabel("|V| [kV]")
    ax1.set_title(f"LIM voltage convergence  (ω = {OMEGA}, converged at iter {bfs_iters})")
    ax1.legend(loc="right")
    ax1.grid(True, linestyle=":")

    # ── Bottom: deviation from converged value ────────────────────────────────
    ax2 = axes[1]
    V_A_conv = lim_history["Sub_A"][-1]
    V_B_conv = lim_history["Sub_B"][-1]
    dV_A = [abs(v - V_A_conv) for v in lim_history["Sub_A"]]
    dV_B = [abs(v - V_B_conv) for v in lim_history["Sub_B"]]
    ax2.semilogy(iters_axis, dV_A, color="tab:blue",   linewidth=1.2, label="Sub_A error")
    ax2.semilogy(iters_axis, dV_B, color="tab:orange", linewidth=1.2, label="Sub_B error")
    ax2.axhline(TOL, color="red", linestyle="--", linewidth=1, label=f"TOL = {TOL}")
    ax2.set_xlabel("LIM iteration")
    ax2.set_ylabel("|ΔV| [kV]  (log scale)")
    ax2.legend(loc="upper right")
    ax2.grid(True, linestyle=":")

    plt.tight_layout()
    plt.show()
