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

OMEGA         = 0.5    # LIM under-relaxation factor
MAX_LIM_OUTER = 5000   # max iterations for LIM bus update (Gauss-Jacobi)
MAX_NR_OUTER  = 200    # max outer branch-current iterations for NR (typically < 10)
MAX_OUTER     = 20     # max outer (transformer ↔ network) iterations
TOL           = 1e-6   # outer convergence threshold [kV]
NR_TOL        = 1e-10  # inner NR convergence threshold [kA]
NR_MAX        = 50     # inner NR iteration cap per bus per outer step


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
# 4.  MV network — LIM (Gauss-Jacobi) solver
#
#  Classic Gauss-Jacobi iteration with under-relaxation (ω = OMEGA):
#    I_in^(k)   = (V_slack − V_bus^(k)) / Z_branch
#    I_load^(k) = conj(S / V_bus^(k))
#    V_bus^(k+1) = V_bus^(k) + ω · (I_in^(k) − I_load^(k)) / Y_self
#
#  Y_self = 1/Z_branch for a single-branch (radial) bus.
#  Converges linearly; mirrors one FMI co-simulation tick per mosaik step.
# ─────────────────────────────────────────────────────────────────────────────

def solve_mv_network_lim(V_slack_kv, record_history=False):
    """
    LIM (Gauss-Jacobi) solver for the 2-branch MV network.
    Each outer iteration = one FMI co-simulation tick in the mosaik scenario.
    """
    V_slack  = complex(V_slack_kv, 0.0)
    Z_A      = complex(LINE_A["r"], LINE_A["x"])
    Z_B      = complex(LINE_B["r"], LINE_B["x"])
    Y_self_A = 1.0 / Z_A
    Y_self_B = 1.0 / Z_B

    V_A = V_slack
    V_B = V_slack

    history = {"MV_Slack": [], "Sub_A": [], "Sub_B": []} if record_history else None

    for iters in range(1, MAX_LIM_OUTER + 1):
        I_A = (V_slack - V_A) / Z_A
        I_B = (V_slack - V_B) / Z_B

        I_load_A = (S_A / V_A).conjugate() if abs(V_A) > 1e-9 else complex(0.0)
        I_load_B = (S_B / V_B).conjugate() if abs(V_B) > 1e-9 else complex(0.0)

        V_A_new = V_A + OMEGA * (I_A - I_load_A) / Y_self_A
        V_B_new = V_B + OMEGA * (I_B - I_load_B) / Y_self_B

        if record_history:
            history["MV_Slack"].append(V_slack_kv)
            history["Sub_A"].append(abs(V_A_new))
            history["Sub_B"].append(abs(V_B_new))

        if max(abs(V_A_new - V_A), abs(V_B_new - V_B)) < TOL:
            V_A, V_B = V_A_new, V_B_new
            break
        V_A, V_B = V_A_new, V_B_new

    I_A      = (V_slack - V_A) / Z_A
    I_B      = (V_slack - V_B) / Z_B
    S_from_A = V_slack * I_A.conjugate()
    S_from_B = V_slack * I_B.conjugate()

    return V_A, V_B, S_from_A, S_from_B, iters, history


# ─────────────────────────────────────────────────────────────────────────────
# 5.  MV network — Newton-Raphson bus solve inside an outer branch-current loop
#
#  The outer loop updates branch currents using previous-step voltages
#  (mirroring the one-step FMI coupling delay in co-simulation):
#    I_A^(k+1) = (V_slack^(k) − V_A^(k)) / Z_A
#    I_B^(k+1) = (V_slack^(k) − V_B^(k)) / Z_B
#
#  The inner NR solve finds the exact bus voltage for the given I_in:
#    f(V) = I_in − conj(S/V) − jB·V = 0
#
#  In rectangular form (V = x+jy, D = x²+y², A = Px+Qy, C = Py−Qx):
#    f_re = I_in_re − A/D + B·y = 0
#    f_im = I_in_im − C/D − B·x = 0
#
#  Jacobian:
#    J11 = (2x·A − P·D) / D²
#    J12 = (2y·A − Q·D) / D² + B
#    J21 = (Q·D + 2x·C) / D² − B
#    J22 = (2y·C − P·D) / D²
#
#  Update: Δx = −(J22·f_re − J12·f_im)/det,  Δy = (J21·f_re − J11·f_im)/det
#
#  Converges quadratically; the outer loop typically takes < 10 iterations.
# ─────────────────────────────────────────────────────────────────────────────

def _nr_bus_solve(I_in, S_load, V_init, B_shunt=0.0):
    """
    Newton-Raphson solve for one bus: find V such that
        I_in = conj(S_load / V) + jB·V

    Parameters
    ----------
    I_in   : complex injected current from adjacent branches [kA]
    S_load : complex load apparent power [MVA]  (P + jQ, positive = consumption)
    V_init : complex initial voltage estimate [kV]  (warm start)
    B_shunt: shunt susceptance [S]  (0 for these lines)

    Returns
    -------
    V : complex converged bus voltage [kV]
    """
    P, Q = S_load.real, S_load.imag
    B    = B_shunt
    x, y = V_init.real, V_init.imag

    for _ in range(NR_MAX):
        D = x * x + y * y
        if D < 1e-18:
            break  # voltage collapsed

        A = P * x + Q * y
        C = P * y - Q * x

        f_re = I_in.real - A / D + B * y
        f_im = I_in.imag - C / D - B * x

        if math.hypot(f_re, f_im) < NR_TOL:
            break  # converged

        D2  = D * D
        J11 = (2.0 * x * A - P * D) / D2
        J12 = (2.0 * y * A - Q * D) / D2 + B
        J21 = (Q * D + 2.0 * x * C) / D2 - B
        J22 = (2.0 * y * C - P * D) / D2

        det = J11 * J22 - J12 * J21
        if abs(det) < 1e-30:
            break  # singular Jacobian

        dx = -(J22 * f_re - J12 * f_im) / det
        dy =  (J21 * f_re - J11 * f_im) / det
        x += dx
        y += dy

    return complex(x, y)


def solve_mv_network_nr(V_slack_kv, record_history=False):
    """
    Newton-Raphson power flow for the 2-branch MV network.

    Solves the full per-bus power flow equation including branch admittance:
        f(V) = (V_slack − V) / Z_branch − conj(S / V) = 0

    Since buses A and B are only connected to the slack (radial topology),
    they are decoupled and solved independently in one NR loop — no outer
    branch-current coupling iteration is needed.

    Full Jacobian (V = x+jy, D=x²+y², A=Px+Qy, C=Py−Qx, r=Z.real, z=Z.imag):
        J11 = −r/|Z|² + (2xA − PD)/D²
        J12 =  z/|Z|² + (2yA − QD)/D²
        J21 =  z/|Z|² + (QD + 2xC)/D²
        J22 = −r/|Z|² + (2yC − PD)/D²

    Returns
    -------
    V_A, V_B           : complex bus voltages [kV]
    S_from_A, S_from_B : complex sending-end apparent power [MVA]
    iters              : NR iterations taken
    history            : dict of lists {bus: [|V| per NR iter]} if record_history
    """
    vs    = V_slack_kv
    Z_A   = complex(LINE_A["r"], LINE_A["x"])
    Z_B   = complex(LINE_B["r"], LINE_B["x"])
    Zsq_A = Z_A.real**2 + Z_A.imag**2
    Zsq_B = Z_B.real**2 + Z_B.imag**2
    P_A, Q_A = S_A.real, S_A.imag
    P_B, Q_B = S_B.real, S_B.imag

    # Flat start at slack voltage (fine for NR with full Jacobian)
    x_A, y_A = vs, 0.0
    x_B, y_B = vs, 0.0

    history = {"MV_Slack": [], "Sub_A": [], "Sub_B": []} if record_history else None

    for iters in range(1, MAX_NR_OUTER + 1):
        # ── Bus A ────────────────────────────────────────────────────────────
        D_A    = x_A*x_A + y_A*y_A
        A_A    = P_A*x_A + Q_A*y_A
        C_A    = P_A*y_A - Q_A*x_A
        f_re_A = ((vs - x_A)*Z_A.real - y_A*Z_A.imag) / Zsq_A - A_A / D_A
        f_im_A = (-(vs - x_A)*Z_A.imag - y_A*Z_A.real) / Zsq_A - C_A / D_A
        D2_A   = D_A * D_A
        J11 = -Z_A.real/Zsq_A + (2*x_A*A_A - P_A*D_A) / D2_A
        J12 = -Z_A.imag/Zsq_A + (2*y_A*A_A - Q_A*D_A) / D2_A
        J21 =  Z_A.imag/Zsq_A + (Q_A*D_A + 2*x_A*C_A) / D2_A
        J22 = -Z_A.real/Zsq_A + (2*y_A*C_A - P_A*D_A) / D2_A
        det_A  = J11*J22 - J12*J21
        if abs(det_A) > 1e-30:
            x_A -= (J22*f_re_A - J12*f_im_A) / det_A
            y_A += (J21*f_re_A - J11*f_im_A) / det_A

        # ── Bus B ────────────────────────────────────────────────────────────
        D_B    = x_B*x_B + y_B*y_B
        A_B    = P_B*x_B + Q_B*y_B
        C_B    = P_B*y_B - Q_B*x_B
        f_re_B = ((vs - x_B)*Z_B.real - y_B*Z_B.imag) / Zsq_B - A_B / D_B
        f_im_B = (-(vs - x_B)*Z_B.imag - y_B*Z_B.real) / Zsq_B - C_B / D_B
        D2_B   = D_B * D_B
        J11b = -Z_B.real/Zsq_B + (2*x_B*A_B - P_B*D_B) / D2_B
        J12b = -Z_B.imag/Zsq_B + (2*y_B*A_B - Q_B*D_B) / D2_B
        J21b =  Z_B.imag/Zsq_B + (Q_B*D_B + 2*x_B*C_B) / D2_B
        J22b = -Z_B.real/Zsq_B + (2*y_B*C_B - P_B*D_B) / D2_B
        det_B  = J11b*J22b - J12b*J21b
        if abs(det_B) > 1e-30:
            x_B -= (J22b*f_re_B - J12b*f_im_B) / det_B
            y_B += (J21b*f_re_B - J11b*f_im_B) / det_B

        if record_history:
            history["MV_Slack"].append(vs)
            history["Sub_A"].append(math.hypot(x_A, y_A))
            history["Sub_B"].append(math.hypot(x_B, y_B))

        if math.hypot(f_re_A, f_im_A) < NR_TOL and math.hypot(f_re_B, f_im_B) < NR_TOL:
            break

    V_A      = complex(x_A, y_A)
    V_B      = complex(x_B, y_B)
    V_slack  = complex(vs, 0.0)
    I_A      = (V_slack - V_A) / Z_A
    I_B      = (V_slack - V_B) / Z_B
    S_from_A = V_slack * I_A.conjugate()
    S_from_B = V_slack * I_B.conjugate()

    return V_A, V_B, S_from_A, S_from_B, iters, history


# ─────────────────────────────────────────────────────────────────────────────
# 6.  Run both methods: transformer outer loop drives each network solver
# ─────────────────────────────────────────────────────────────────────────────

def run_outer_loop(solver_fn, label):
    """Converge transformer ↔ MV network using the given network solver."""
    V_mv_kv = TR["ratedU2"] / 1e3   # flat start: 20.0 kV nominal
    V_A = V_B = complex(V_mv_kv, 0.0)
    S_from_A = S_from_B = complex(0.0)
    P2_mw = Q2_mvar = 0.0
    inner_iters = outer_iters = 0
    history = None

    print(f"\n{'─'*68}")
    print(f"  {label}")
    print(f"  {'Outer':>5}  {'V_mv kV':>11}  {'P2 MW':>8}  {'Q2 MVAr':>9}  {'inner':>7}")

    for outer in range(1, MAX_OUTER + 1):
        V_A, V_B, S_from_A, S_from_B, inner_iters, _ = solver_fn(V_mv_kv)
        S_total  = S_from_A + S_from_B
        P2_mw    = S_total.real
        Q2_mvar  = S_total.imag
        V_mv_new = transformer_v2(HV_SLACK_KV, HV_SLACK_DEG, P2_mw, Q2_mvar)
        outer_iters = outer
        print(f"  {outer:>5}  {V_mv_new:>11.6f}  {P2_mw:>8.4f}  {Q2_mvar:>9.4f}  {inner_iters:>7}")
        if abs(V_mv_new - V_mv_kv) < TOL:
            V_mv_kv = V_mv_new
            V_mv_kv = V_mv_new
            break
        V_mv_kv = V_mv_new

    # Always capture history for plotting (converged or not)
    V_A, V_B, S_from_A, S_from_B, inner_iters, history = solver_fn(
        V_mv_kv, record_history=True
    )

    return dict(
        V_mv=V_mv_kv, V_A=V_A, V_B=V_B,
        P2=P2_mw, Q2=Q2_mvar,
        outer=outer_iters, inner=inner_iters,
        history=history,
    )


res_lim = run_outer_loop(solve_mv_network_lim, "LIM — Gauss-Jacobi  (ω = 0.5)")
res_nr  = run_outer_loop(solve_mv_network_nr,  "NR  — Newton-Raphson inner solve")

# ─────────────────────────────────────────────────────────────────────────────
# 7.  Comparison table
# ─────────────────────────────────────────────────────────────────────────────

print()
print("═" * 68)
print(f"  {'Quantity':<26}  {'LIM':>16}  {'NR':>16}")
print(f"  {'─'*26}  {'─'*16}  {'─'*16}")
print(f"  {'Transformer V2 [kV]':<26}  {res_lim['V_mv']:>16.6f}  {res_nr['V_mv']:>16.6f}")
print(f"  {'Sub_A |V| [kV]':<26}  {abs(res_lim['V_A']):>16.6f}  {abs(res_nr['V_A']):>16.6f}")
print(f"  {'Sub_A angle [°]':<26}  {math.degrees(cmath.phase(res_lim['V_A'])):>16.6f}  {math.degrees(cmath.phase(res_nr['V_A'])):>16.6f}")
print(f"  {'Sub_B |V| [kV]':<26}  {abs(res_lim['V_B']):>16.6f}  {abs(res_nr['V_B']):>16.6f}")
print(f"  {'Sub_B angle [°]':<26}  {math.degrees(cmath.phase(res_lim['V_B'])):>16.6f}  {math.degrees(cmath.phase(res_nr['V_B'])):>16.6f}")
print(f"  {'Total P2 [MW]':<26}  {res_lim['P2']:>16.6f}  {res_nr['P2']:>16.6f}")
print(f"  {'Total Q2 [MVAr]':<26}  {res_lim['Q2']:>16.6f}  {res_nr['Q2']:>16.6f}")
print(f"  {'Outer iters (transformer)':<26}  {res_lim['outer']:>16}  {res_nr['outer']:>16}")
print(f"  {'GJ/NR iters (final call)':<26}  {res_lim['inner']:>16}  {res_nr['inner']:>16}")
print(f"  {'|V_A| difference [kV]':<26}  {abs(abs(res_lim['V_A']) - abs(res_nr['V_A'])):>34.2e}")
print(f"  {'|V_B| difference [kV]':<26}  {abs(abs(res_lim['V_B']) - abs(res_nr['V_B'])):>34.2e}")
print("═" * 68)

# ─────────────────────────────────────────────────────────────────────────────
# 8.  Convergence comparison plot  (2 × 2 grid)
# ─────────────────────────────────────────────────────────────────────────────

fig, axes = plt.subplots(2, 2, figsize=(14, 8))
fig.suptitle(
    "LIM (Gauss-Jacobi) vs Newton-Raphson — Outer Iteration Convergence",
    fontsize=13,
)

methods = [
    ("LIM  (Gauss-Jacobi, ω=0.5)",      res_lim["history"], res_lim["inner"], "GJ iteration"),
    ("NR   (Newton-Raphson power flow)",  res_nr["history"],  res_nr["inner"],  "NR iteration"),
]

for col, (title, hist, n_iters, xlabel) in enumerate(methods):
    ax_top = axes[0][col]
    ax_bot = axes[1][col]
    n = len(hist["Sub_A"])
    iters_axis = list(range(1, n + 1))

    # ── Top: absolute voltage magnitude ──────────────────────────────────
    ax_top.plot(iters_axis, hist["MV_Slack"], color="tab:green",  lw=1.5, label="MV_Slack")
    ax_top.plot(iters_axis, hist["Sub_A"],    color="tab:blue",   lw=1.2, label="Sub_A")
    ax_top.plot(iters_axis, hist["Sub_B"],    color="tab:orange", lw=1.2, label="Sub_B")
    ax_top.set_title(f"{title}\n(converged at iter {n_iters})")
    ax_top.set_ylabel("|V| [kV]")
    ax_top.legend(loc="right")
    ax_top.grid(True, linestyle=":")

    # ── Bottom: deviation from converged value (log scale) ───────────────
    V_A_conv = hist["Sub_A"][-1]
    V_B_conv = hist["Sub_B"][-1]
    dV_A = [max(abs(v - V_A_conv), 1e-16) for v in hist["Sub_A"]]
    dV_B = [max(abs(v - V_B_conv), 1e-16) for v in hist["Sub_B"]]
    ax_bot.semilogy(iters_axis, dV_A, color="tab:blue",   lw=1.2, label="Sub_A error")
    ax_bot.semilogy(iters_axis, dV_B, color="tab:orange", lw=1.2, label="Sub_B error")
    ax_bot.axhline(TOL, color="red", linestyle="--", lw=1, label=f"TOL={TOL}")
    ax_bot.set_xlabel(xlabel)
    ax_bot.set_ylabel("|ΔV| [kV]  (log scale)")
    ax_bot.legend(loc="upper right")
    ax_bot.grid(True, linestyle=":")

plt.tight_layout()
plt.show()
