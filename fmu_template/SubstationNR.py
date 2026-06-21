import cmath
import math

from component_model.model import Model
from component_model.variable import Variable


class SubstationNR(Model):
    """IEEE 14-bus substation bus FMU — Newton-Raphson phasor-domain power flow.

    Same FMI interface as Substation (LIM/Gauss-Jacobi). Each do_step() call
    runs Newton-Raphson to convergence for the fixed I_in provided by the line
    FMUs, rather than performing a single relaxed Gauss-Jacobi update.

    NR solves the local KCL equation:
        f(V) = I_in − conj(S/V) − jB·V = 0

    In rectangular form (V = x + jy, D = x²+y², A = Px+Qy, C = Py−Qx):
        f_re = I_in_re − A/D + B·y = 0
        f_im = I_in_im − C/D − B·x = 0

    Jacobian (analytically derived):
        J11 = ∂f_re/∂x = (2x·A − P·D) / D²
        J12 = ∂f_re/∂y = (2y·A − Q·D) / D² + B
        J21 = ∂f_im/∂x = (Q·D + 2x·C) / D² − B
        J22 = ∂f_im/∂y = (2y·C − P·D) / D²

    Newton update:
        det  = J11·J22 − J12·J21
        Δx   = −(J22·f_re − J12·f_im) / det
        Δy   =  (J21·f_re − J11·f_im) / det

    Converges quadratically; typically 3–6 iterations from a warm start.
    omega_relax provides outer Gauss-Jacobi damping: after the inner NR
    converges to V_NR, the output is blended as
        V_output = V_old + omega_relax * (V_NR − V_old)
    This prevents the outer GJ iteration from diverging when both line
    endpoints use time-shifted (previous-tick) voltages.  Use omega_relax=0.5
    (same as the LIM Substation FMU) for stability.

    Parameters:
        Y_self_re   — real part of bus self-admittance  Σ y_ij  [S]  (unused in NR)
        Y_self_im   — imaginary part of bus self-admittance  Σ y_ij  [S]  (unused in NR)
        B_shunt     — total shunt susceptance at this bus: Σ bch/2  [S]
        omega_relax — outer GJ damping factor applied after NR convergence (0 < ω ≤ 1)
        is_slack    — 1.0 = slack (voltage-reference) bus, 0.0 = load bus
        V_slack_kv  — slack bus voltage magnitude  [kV]
        V_slack_ang_deg — slack bus reference angle  [deg]
        is_sync_machine — 1.0 = SynchronousMachine bus (regulated |V|, free angle)
        V_reg_kv    — voltage regulation setpoint for SynchronousMachine bus  [kV]

    Inputs:
        I_in_mag    — magnitude of net current injected by adjacent LineFMUs  [kA]
        I_in_ang    — phase angle of net current injected by adjacent LineFMUs  [deg]
        P_load_mw   — active power demand at this bus  [MW]
        Q_load_mvar — reactive power demand at this bus  [MVAr]

    Outputs:
        V_mag_kv    — bus voltage magnitude |V|  [kV]
        V_ang_deg   — bus voltage angle  [deg]
    """

    def __init__(
        self,
        name: str = "SubstationNR",
        description: str = "IEEE 14-bus substation bus FMU — Newton-Raphson phasor power flow.",
        Y_self_re: float = 0.0,
        Y_self_im: float = 0.0,
        B_shunt: float = 0.0,
        omega_relax: float = 0.5,       # unused — kept for drop-in compatibility
        is_slack: float = 0.0,
        V_slack_kv: float = 0.0,
        V_slack_ang_deg: float = 0.0,
        is_sync_machine: float = 0.0,
        V_reg_kv: float = 0.0,
        I_in_mag: float = 0.0,
        I_in_ang: float = 0.0,
        P_load_mw: float = 0.0,
        Q_load_mvar: float = 0.0,
        V_mag_kv: float = 0.0,
        V_ang_deg: float = 0.0,
        **kwargs,
    ):
        super().__init__(name, description, author="Viet Hung Pham", **kwargs)

        self._Y_self_re       = self._interface("Y_self_re",       Y_self_re)
        self._Y_self_im       = self._interface("Y_self_im",       Y_self_im)
        self._B_shunt         = self._interface("B_shunt",         B_shunt)
        self._omega_relax     = self._interface("omega_relax",     omega_relax)  # outer GJ damping
        self._is_slack        = self._interface("is_slack",        is_slack)
        self._V_slack_kv      = self._interface("V_slack_kv",      V_slack_kv)
        self._V_slack_ang_deg = self._interface("V_slack_ang_deg", V_slack_ang_deg)
        self._is_sync_machine = self._interface("is_sync_machine", is_sync_machine)
        self._V_reg_kv        = self._interface("V_reg_kv",        V_reg_kv)
        self._I_in_mag        = self._interface("I_in_mag",        I_in_mag)
        self._I_in_ang        = self._interface("I_in_ang",        I_in_ang)
        self._P_load_mw       = self._interface("P_load_mw",       P_load_mw)
        self._Q_load_mvar     = self._interface("Q_load_mvar",     Q_load_mvar)
        self._V_mag_kv        = self._interface("V_mag_kv",        V_mag_kv)
        self._V_ang_deg       = self._interface("V_ang_deg",       V_ang_deg)

        self.time = 0.0
        self._V = cmath.rect(V_slack_kv, math.radians(V_slack_ang_deg))

    def setup_experiment(self, start_time: float, stop_time: float = None, tolerance: float = None):
        self.time = start_time
        self._V = cmath.rect(self.V_slack_kv, math.radians(self.V_slack_ang_deg))
        self._sync_outputs()

    def exit_initialization_mode(self):
        super().exit_initialization_mode()
        self._V = cmath.rect(self.V_slack_kv, math.radians(self.V_slack_ang_deg))
        self._sync_outputs()

    def do_step(self, current_time: float, step_size: float) -> bool:
        """
        Solve the bus voltage update via one of two formulations:

        PQ bus (load bus):
            Solve f(V) = I_nbr − conj(S/V) − Y_self·V − jB·V = 0 via rectangular NR.
            I_nbr = I_in + Y_self·V_old removes the self-coupling from time-shifted FMUs.

        PV bus (SynchronousMachine):
            Find angle θ analytically at |V| = V_reg satisfying the real-power balance:
                P_sch = V_reg·(I_nbr_re·cosθ + I_nbr_im·sinθ) − V_reg²·Y_re
            Rearranged: I_nbr_re·cosθ + I_nbr_im·sinθ = K,  K = (P_sch + V_reg²·Y_re)/V_reg
            Direct solution: θ = ∠I_nbr ± arccos(K/|I_nbr|); take root closest to warm-start.
            Reactive power Q is FREE — the machine absorbs/injects whatever Q is required.
            This is the correct PV-bus formulation; the old fixed-Q approach converged to
            a different operating point than the CIM power flow.

        Both paths apply outer GJ relaxation:
            V_output = V_old + omega_relax·(V_NR − V_old)
        For sync machines |V| is pinned to V_reg after relaxation.
        """
        NR_TOL      = 1e-10   # [kA] convergence tolerance on |f(V)|
        NR_MAX_ITER = 50      # safety cap on inner iterations

        if self.is_slack >= 0.5:
            # Slack bus: voltage fixed at reference — no solve needed.
            self._V = cmath.rect(self.V_slack_kv, math.radians(self.V_slack_ang_deg))
        else:
            I_in = cmath.rect(self.I_in_mag, math.radians(self.I_in_ang))
            P    = self.P_load_mw
            Q    = self.Q_load_mvar
            B    = self.B_shunt
            Y_re = self.Y_self_re
            Y_im = self.Y_self_im

            V_old = complex(self._V)  # save for outer Gauss-Jacobi relaxation

            # Remove self-coupling from I_in so the NR solves the full bus equation:
            #   I_nbr = conj(S/V) + Y_self·V + jB·V
            # I_nbr = Σ_{j≠i} y_ij·V_j^prev  (neighbor contributions only)
            I_nbr = I_in + complex(Y_re, Y_im) * V_old

            if self.is_sync_machine >= 0.5 and self.V_reg_kv > 1e-9:
                # ── PV-bus: analytical polar solve ──────────────────────────────
                # Real power balance (B_shunt contributes zero to real power):
                #   P_sch = V_reg·(I_re·cosθ + I_im·sinθ) − V_reg²·Y_re
                # → I_re·cosθ + I_im·sinθ = K
                V_reg = self.V_reg_kv
                K     = (P + V_reg * V_reg * Y_re) / V_reg
                I_mag = abs(I_nbr)
                if I_mag < 1e-15:
                    # No neighbour current: keep old angle
                    V_NR = cmath.rect(V_reg, cmath.phase(V_old))
                else:
                    ratio   = max(-1.0, min(1.0, K / I_mag))   # clamp for arccos
                    phi_I   = cmath.phase(I_nbr)
                    delta   = math.acos(ratio)
                    theta_1 = phi_I + delta
                    theta_2 = phi_I - delta
                    theta_old = cmath.phase(V_old)
                    # Pick the root closest to the warm-start angle
                    if abs(theta_1 - theta_old) <= abs(theta_2 - theta_old):
                        theta = theta_1
                    else:
                        theta = theta_2
                    V_NR = cmath.rect(V_reg, theta)

            else:
                # ── PQ-bus: rectangular Newton-Raphson ──────────────────────────
                x, y = V_old.real, V_old.imag

                for _ in range(NR_MAX_ITER):
                    D = x * x + y * y
                    if D < 1e-18:
                        break  # voltage collapsed to zero — cannot invert

                    A = P * x + Q * y  # = |V|² · Re(conj(S/V))
                    C = P * y - Q * x  # = |V|² · Im(conj(S/V))

                    # Residual f(V) = I_nbr − conj(S/V) − Y_self·V − jB·V
                    f_re = I_nbr.real - A / D - Y_re * x + Y_im * y + B * y
                    f_im = I_nbr.imag - C / D - Y_re * y - Y_im * x - B * x

                    if math.hypot(f_re, f_im) < NR_TOL:
                        break  # converged

                    # Analytical Jacobian ∂[f_re, f_im]/∂[x, y]
                    D2  = D * D
                    J11 = (2.0 * x * A - P * D) / D2
                    J12 = (2.0 * y * A - Q * D) / D2
                    J21 = (Q * D + 2.0 * x * C) / D2
                    J22 = (2.0 * y * C - P * D) / D2
                    J11 -= Y_re
                    J12 += Y_im + B
                    J21 -= Y_im + B
                    J22 -= Y_re

                    det = J11 * J22 - J12 * J21
                    if abs(det) < 1e-30:
                        break  # singular Jacobian — stop

                    dx = -(J22 * f_re - J12 * f_im) / det
                    dy =  (J21 * f_re - J11 * f_im) / det
                    x += dx
                    y += dy

                V_NR = complex(x, y)

            # Outer Gauss-Jacobi relaxation.
            omega = self.omega_relax
            if omega < 1.0 - 1e-9:
                self._V = V_old + omega * (V_NR - V_old)
            else:
                self._V = V_NR

            # SynchronousMachine bus: pin |V| to regulation setpoint, keep angle
            if self.is_sync_machine >= 0.5 and self.V_reg_kv > 1e-9:
                self._V = cmath.rect(self.V_reg_kv, cmath.phase(self._V))

        self._sync_outputs()
        return True

    def _sync_outputs(self):
        """Write internal complex state to the scalar FMI output variables."""
        self.V_mag_kv  = abs(self._V)
        self.V_ang_deg = math.degrees(cmath.phase(self._V))

    def _interface(self, name: str, start: float) -> Variable:
        """Register one FMU2 interface variable."""
        _defs = {
            "Y_self_re":       ("Real part of bus self-admittance Σ y_ij [S] (unused in NR)",
                                "parameter", "fixed", None),
            "Y_self_im":       ("Imaginary part of bus self-admittance Σ y_ij [S] (unused in NR)",
                                "parameter", "fixed", None),
            "B_shunt":         ("Total shunt susceptance at this bus Σ bch/2 [S]",
                                "parameter", "fixed", None),
            "omega_relax":     ("Under-relaxation factor (unused in NR; kept for compatibility)",
                                "parameter", "fixed", None),
            "is_slack":        ("1.0 = slack (reference) bus, 0.0 = load bus",
                                "parameter", "fixed", None),
            "V_slack_kv":      ("Slack bus voltage magnitude [kV]",
                                "parameter", "fixed", None),
            "V_slack_ang_deg": ("Slack bus reference voltage angle [deg]",
                                "parameter", "fixed", None),
            "is_sync_machine": ("1.0 = SynchronousMachine bus (regulated |V|, free angle)",
                                "parameter", "fixed", None),
            "V_reg_kv":        ("Voltage regulation setpoint [kV] for SynchronousMachine bus",
                                "parameter", "fixed", None),
            "I_in_mag":        ("Net injected current magnitude from LineFMUs [kA]",
                                "input", "continuous", None),
            "I_in_ang":        ("Net injected current angle from LineFMUs [deg]",
                                "input", "continuous", None),
            "P_load_mw":       ("Active power demand [MW]",
                                "input", "continuous", None),
            "Q_load_mvar":     ("Reactive power demand [MVAr]",
                                "input", "continuous", None),
            "V_mag_kv":        ("Bus voltage magnitude |V| [kV]",
                                "output", "continuous", "calculated"),
            "V_ang_deg":       ("Bus voltage angle [deg]",
                                "output", "continuous", "calculated"),
        }
        if name not in _defs:
            raise KeyError(f"Interface variable '{name}' not defined") from None

        description, causality, variability, initial = _defs[name]
        kwargs = dict(
            name=name,
            description=description,
            causality=causality,
            variability=variability,
            start=start,
            rng=(),
        )
        if initial is not None:
            kwargs["initial"] = initial
        return Variable(self, **kwargs)
