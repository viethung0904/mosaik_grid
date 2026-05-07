import cmath
import math

from component_model.model import Model
from component_model.variable import Variable


class Substation(Model):
    """CIGRE MV Network substation bus FMU — LIM-based phasor-domain power flow.

    Each bus in the CIGRE MV grid becomes one instance of this FMU.
    The one-step FMI communication delay between SubstationFMUs and LineFMUs
    provides the LIM latency inherently — no extra numerical trick is needed.

    LIM update (under-relaxed Jacobi):
        I_load   = conj(S_load / V^(k))
        I_shunt  = jB_shunt · V^(k)
        residual = I_in^(k+1) − I_load − I_shunt
        V^(k+1)  = V^(k) + ω · residual / Y_self

    Why ω < 1 is required (radial / tree networks):
        bch ≈ 0  →  Y_off ≈ Y_self  →  spectral radius ρ ≥ 1 for pure Jacobi.
        ω = 0.5 gives ρ_eff ≈ 0.5, converging in ~3500 FMI steps.

    Parameters:
        Y_self_re   — real part of bus self-admittance  Σ y_ij  [S]
        Y_self_im   — imaginary part of bus self-admittance  Σ y_ij  [S]
        B_shunt     — total shunt susceptance at this bus: Σ bch/2  [S]
                      Set to 0.0 when using self-contained ACLineSegment FMU
                      (pi-model shunt already handled by the line FMU).
        omega_relax — under-relaxation factor (0.5 recommended for tree networks)
        is_slack    — 1.0 = slack (voltage-reference) bus, 0.0 = load bus
        V_slack_kv  — slack bus voltage magnitude  [kV]

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
        name: str = "Substation",
        description: str = "CIGRE MV substation bus FMU — LIM phasor power flow.",
        Y_self_re: float = 0.0,        # S   — real part of Σ y_ij
        Y_self_im: float = 0.0,        # S   — imaginary part of Σ y_ij
        B_shunt: float = 0.0,          # S   — total shunt susceptance (0.0 when ACLineSegment handles shunt)
        omega_relax: float = 0.5,      #     — under-relaxation factor
        is_slack: float = 0.0,         #     — 1.0 = slack bus, 0.0 = load bus
        V_slack_kv: float = 0.0,       # kV  — slack voltage magnitude
        V_slack_ang_deg: float = 0.0,  # deg — slack bus reference angle (0.0 for global slack)
        is_pv: float = 0.0,            #     — 1.0 = PV bus (scheduled P, regulated |V|, free angle)
        V_pv_kv: float = 0.0,          # kV  — target |V| magnitude for PV bus
        I_in_mag: float = 0.0,         # kA  — net injected current magnitude (input)
        I_in_ang: float = 0.0,         # deg — net injected current angle (input)
        P_load_mw: float = 0.0,        # MW  — active load demand (input)
        Q_load_mvar: float = 0.0,      # MVAr— reactive load demand (input)
        V_mag_kv: float = 0.0,         # kV  — |V| magnitude (output)
        V_ang_deg: float = 0.0,        # deg — voltage angle (output)
        **kwargs,
    ):
        super().__init__(name, description, author="Viet Hung Pham", **kwargs)

        # Parameters
        self._Y_self_re    = self._interface("Y_self_re",    Y_self_re)
        self._Y_self_im    = self._interface("Y_self_im",    Y_self_im)
        self._B_shunt      = self._interface("B_shunt",      B_shunt)
        self._omega_relax  = self._interface("omega_relax",  omega_relax)
        self._is_slack       = self._interface("is_slack",       is_slack)
        self._V_slack_kv     = self._interface("V_slack_kv",     V_slack_kv)
        self._V_slack_ang_deg= self._interface("V_slack_ang_deg",V_slack_ang_deg)
        self._is_pv          = self._interface("is_pv",          is_pv)
        self._V_pv_kv        = self._interface("V_pv_kv",        V_pv_kv)

        # Inputs
        self._I_in_mag     = self._interface("I_in_mag",     I_in_mag)
        self._I_in_ang     = self._interface("I_in_ang",     I_in_ang)
        self._P_load_mw    = self._interface("P_load_mw",    P_load_mw)
        self._Q_load_mvar  = self._interface("Q_load_mvar",  Q_load_mvar)

        # Outputs
        self._V_mag_kv     = self._interface("V_mag_kv",     V_mag_kv)
        self._V_ang_deg    = self._interface("V_ang_deg",    V_ang_deg)

        self.time = 0.0
        # Internal complex voltage state — not an FMI variable (FMI is scalar-only)
        self._V = cmath.rect(V_slack_kv, math.radians(V_slack_ang_deg))

    def setup_experiment(self, start_time: float, stop_time: float = None, tolerance: float = None):
        self.time = start_time
        self._V = cmath.rect(self.V_slack_kv, math.radians(self.V_slack_ang_deg))
        self._sync_outputs()

    def exit_initialization_mode(self):
        super().exit_initialization_mode()
        # Re-sync internal voltage now that parameters have been set via FMI setReal()
        self._V = cmath.rect(self.V_slack_kv, math.radians(self.V_slack_ang_deg))
        self._sync_outputs()

    def do_step(self, current_time: float, step_size: float) -> bool:
        """
        One LIM Jacobi iteration for this bus node.

        For the phasor-domain case, step_size is dimensionless (= one iteration).
        For EMT-domain use, step_size = H seconds; H must satisfy
            H < 0.9 · min_branch(2√(L·C))
        which for the CIGRE MV grid is H_max ≈ 5.8 µs
        (see lim_stability_dt() in grid_sim.py).
        """
        if self.is_slack >= 0.5:
            # Slack bus: voltage fixed at reference — no update needed.
            self._V = cmath.rect(self.V_slack_kv, math.radians(self.V_slack_ang_deg))
        else:
            Y_self = complex(self.Y_self_re, self.Y_self_im)
            I_in   = cmath.rect(self.I_in_mag, math.radians(self.I_in_ang))
            V_prev = self._V

            # Constant-power load current (uses previous-step voltage)
            S_load = complex(self.P_load_mw, self.Q_load_mvar)
            if abs(V_prev) > 1e-9:
                I_load = (S_load / V_prev).conjugate()
            else:
                I_load = complex(0.0, 0.0)

            # Shunt capacitive current
            I_shunt = complex(0.0, self.B_shunt) * V_prev

            # KCL residual
            residual = I_in - I_load - I_shunt

            # Under-relaxed Jacobi bus update (LIM step)
            if abs(Y_self) > 1e-12:
                self._V = V_prev + self.omega_relax * residual / Y_self

            # PV bus: enforce voltage magnitude setpoint, keep computed angle
            if self.is_pv >= 0.5 and self.V_pv_kv > 1e-9:
                self._V = cmath.rect(self.V_pv_kv, cmath.phase(self._V))

        self._sync_outputs()
        return True

    def _sync_outputs(self):
        """Write internal complex state to the scalar FMI output variables."""
        self.V_mag_kv  = abs(self._V)
        self.V_ang_deg = math.degrees(cmath.phase(self._V))

    def _interface(self, name: str, start: float) -> Variable:
        """Register one FMU2 interface variable."""
        _defs = {
            # name: (description, causality, variability, initial)
            "Y_self_re":   ("Real part of bus self-admittance Σ y_ij [S]",
                            "parameter", "fixed", None),
            "Y_self_im":   ("Imaginary part of bus self-admittance Σ y_ij [S]",
                            "parameter", "fixed", None),
            "B_shunt":     ("Total shunt susceptance at this bus Σ bch/2 [S]",
                            "parameter", "fixed", None),
            "omega_relax": ("Under-relaxation factor for Jacobi LIM update",
                            "parameter", "fixed", None),
            "is_slack":        ("1.0 = slack (reference) bus, 0.0 = load bus",
                                "parameter", "fixed", None),
            "V_slack_kv":      ("Slack bus voltage magnitude [kV]",
                                "parameter", "fixed", None),
            "V_slack_ang_deg": ("Slack bus reference voltage angle [deg]",
                                "parameter", "fixed", None),
            "is_pv":           ("1.0 = PV bus (scheduled P, regulated |V|, free angle)",
                                "parameter", "fixed", None),
            "V_pv_kv":         ("PV bus target voltage magnitude [kV]",
                                "parameter", "fixed", None),
            "I_in_mag":    ("Magnitude of net current injected by adjacent LineFMUs [kA]",
                            "input", "continuous", None),
            "I_in_ang":    ("Phase angle of net current injected by adjacent LineFMUs [deg]",
                            "input", "continuous", None),
            "P_load_mw":   ("Active power demand at this bus [MW]",
                            "input", "continuous", None),
            "Q_load_mvar": ("Reactive power demand at this bus [MVAr]",
                            "input", "continuous", None),
            "V_mag_kv":    ("Bus voltage magnitude |V| [kV]",
                            "output", "continuous", "calculated"),
            "V_ang_deg":   ("Bus voltage angle [deg]",
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
