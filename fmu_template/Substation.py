import cmath
import math

from component_model.model import Model
from component_model.variable import Variable


class Substation(Model):
    """IEEE 14-bus substation bus FMU — LIM (Gauss-Jacobi) phasor-domain power flow.

    Each bus in the network becomes one instance of this FMU.
    Each mosaik tick performs ONE relaxed Gauss-Jacobi iteration:

        ΔV = ω · (I_in − I_load(V)) / Y_self
        V_new = V_old + ΔV

    where I_load = conj(S/V) + jB·V  is the constant-power load current,
    Y_self = Σ y_ij  is the bus self-admittance (series terms only),
    and ω = omega_relax (0.5 recommended for meshed networks).

    The fixed-point condition (I_in = I_load) is equivalent to KCL, so the
    steady-state solution is the correct power-flow result.  N_LIM mosaik
    ticks are executed per physical time step; the time_shifted=True delay on
    Sub→Line connections enforces the Gauss-Jacobi one-step ordering.

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
        is_sync_machine: float = 0.0,   #     — 1.0 = SynchronousMachine bus (scheduled P, regulated |V|, free angle)
        V_reg_kv: float = 0.0,         # kV  — voltage regulation setpoint for SynchronousMachine bus
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
        self._is_sync_machine  = self._interface("is_sync_machine",  is_sync_machine)
        self._V_reg_kv       = self._interface("V_reg_kv",       V_reg_kv)

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
        LIM (Gauss-Jacobi) bus voltage updater.

        Each call performs ONE relaxed Gauss-Jacobi step:

            ΔV = ω · (I_in − I_load(V)) / Y_self
            V_new = V_old + ΔV

        where I_load = conj(S/V) + jB·V  is the load current at the previous voltage,
        Y_self = Y_self_re + j·Y_self_im is the bus self-admittance (Σ 1/Z_k, series only),
        and ω = omega_relax (0.5 recommended for meshed networks).

        This replaces the previous per-step Newton-Raphson solver.  NR with fixed I_in
        finds the exact root of  I_in = conj(S/V)  for whatever I_in the line FMUs
        provide — including the wildly wrong I_in that arises during outer-loop
        transients — which sends V to non-physical roots (e.g. 248 MV).  The LIM
        update is bounded by |ΔV| ~ |residual| / |Y_self|, so it remains stable even
        when I_in is temporarily wrong.

        The ACLineSegment pi-model delivers I_in = Σ I_to_k = Σ y_k·(V_k−V_i) − jΣ(bch_k/2)·V_i.
        The LIM fixed-point condition I_in = I_load is equivalent to KCL, so the
        steady-state solution is the correct power-flow result.
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
            "is_sync_machine":  ("1.0 = SynchronousMachine bus (scheduled P, regulated |V|, free angle)",
                                "parameter", "fixed", None),
            "V_reg_kv":        ("Voltage regulation setpoint [kV] for SynchronousMachine bus",
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
