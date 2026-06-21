import cmath
import math

from component_model.model import Model
from component_model.variable import Variable


class TransformerBranch(Model):
    """Transformer branch FMU — LIM-compatible π current-injection model.

    Models a two-winding transformer as a current-injection branch suitable
    for use in the LIM (Linear Iteration Method) co-simulation master.

    Mathematical model (standard physical transformer, series impedance referred to HV):
        Turns ratio (real, phaseAngleClock = 0 for IEEE 14-bus):
            t = rated_u1_kv / rated_u2_kv

        Series admittance referred to HV side:
            y_s = 1 / (r_hv_ohm + j·x_hv_ohm)   [= y_HV]

        Physical nodal admittance matrix in kV/kA units:
            [ I_HV ]   [ -y_s      t·y_s  ] [ V_HV ]
            [ I_LV ] = [  t·y_s  -t²·y_s  ] [ V_LV ]

        Note: this is the STANDARD physical transformer model (not per-unit).
        Current injected INTO each bus (LIM KCL sign convention):

            I_in_HV = -y_s·V_HV + t·y_s·V_LV
            I_in_LV = +t·y_s·V_HV - t²·y_s·V_LV

        Both voltages are taken from the previous LIM iteration (time_shifted=True
        in the mosaik connection), providing the required one-step Jacobi delay.

    Y_self contributions (added in scenario.py, NOT in this FMU):
        Y_self_HV += y_s          (= y_HV)
        Y_self_LV += y_s · t²    (= y_LV, LV-referred admittance)

    Parameters:
        r_hv_ohm    — series resistance referred to HV side  [Ω]  (usually 0)
        x_hv_ohm    — series reactance referred to HV side   [Ω]
        rated_u1_kv — rated HV voltage  [kV]
        rated_u2_kv — rated LV voltage  [kV]

    Inputs (time-shifted from previous LIM step):
        V_hv_mag_kv  — HV bus voltage magnitude  [kV]
        V_hv_ang_deg — HV bus voltage angle       [deg]
        V_lv_mag_kv  — LV bus voltage magnitude  [kV]
        V_lv_ang_deg — LV bus voltage angle       [deg]

    Outputs (current injections into HV and LV buses):
        I_hv_in_re   — Re(I_in_HV)  [kA]
        I_hv_in_im   — Im(I_in_HV)  [kA]
        I_lv_in_re   — Re(I_in_LV)  [kA]
        I_lv_in_im   — Im(I_in_LV)  [kA]
        P_loss_mw    — Active power loss in series impedance  I²·R  [MW]
        Q_loss_mvar  — Reactive power loss in series impedance I²·X  [MVAr]
    """

    def __init__(
        self,
        name: str = "TransformerBranch",
        description: str = "Transformer branch FMU — LIM pi current-injection model.",
        r_hv_ohm: float = 0.0,        # Ω   — series resistance referred to HV
        x_hv_ohm: float = 1.0,        # Ω   — series reactance referred to HV
        rated_u1_kv: float = 69.0,    # kV  — rated HV voltage
        rated_u2_kv: float = 13.8,    # kV  — rated LV voltage
        V_hv_mag_kv: float = 0.0,     # kV  — HV bus voltage magnitude (input)
        V_hv_ang_deg: float = 0.0,    # deg — HV bus voltage angle (input)
        V_lv_mag_kv: float = 0.0,     # kV  — LV bus voltage magnitude (input)
        V_lv_ang_deg: float = 0.0,    # deg — LV bus voltage angle (input)
        I_hv_in_re: float = 0.0,      # kA  — Re(I injected into HV bus) (output)
        I_hv_in_im: float = 0.0,      # kA  — Im(I injected into HV bus) (output)
        I_lv_in_re: float = 0.0,      # kA  — Re(I injected into LV bus) (output)
        I_lv_in_im: float = 0.0,      # kA  — Im(I injected into LV bus) (output)
        P_loss_mw: float = 0.0,       # MW  — active power loss (output)
        Q_loss_mvar: float = 0.0,     # MVAr— reactive power loss (output)
        **kwargs,
    ):
        super().__init__(name, description, author="Viet Hung Pham", **kwargs)

        # Parameters
        self._r_hv_ohm    = self._interface("r_hv_ohm",    r_hv_ohm)
        self._x_hv_ohm    = self._interface("x_hv_ohm",    x_hv_ohm)
        self._rated_u1_kv = self._interface("rated_u1_kv", rated_u1_kv)
        self._rated_u2_kv = self._interface("rated_u2_kv", rated_u2_kv)

        # Inputs
        self._V_hv_mag_kv  = self._interface("V_hv_mag_kv",  V_hv_mag_kv)
        self._V_hv_ang_deg = self._interface("V_hv_ang_deg", V_hv_ang_deg)
        self._V_lv_mag_kv  = self._interface("V_lv_mag_kv",  V_lv_mag_kv)
        self._V_lv_ang_deg = self._interface("V_lv_ang_deg", V_lv_ang_deg)

        # Outputs
        self._I_hv_in_re  = self._interface("I_hv_in_re",  I_hv_in_re)
        self._I_hv_in_im  = self._interface("I_hv_in_im",  I_hv_in_im)
        self._I_lv_in_re  = self._interface("I_lv_in_re",  I_lv_in_re)
        self._I_lv_in_im  = self._interface("I_lv_in_im",  I_lv_in_im)
        self._P_loss_mw   = self._interface("P_loss_mw",   P_loss_mw)
        self._Q_loss_mvar = self._interface("Q_loss_mvar", Q_loss_mvar)

        self.time = 0.0

    def do_step(self, current_time: float, step_size: float) -> bool:
        """Compute transformer current injections for one LIM Jacobi iteration.

        Uses V_HV and V_LV from the previous iteration (time_shifted inputs).
        Units: kV / Ω = kA (consistent throughout).
        """
        V_hv = cmath.rect(self.V_hv_mag_kv, math.radians(self.V_hv_ang_deg))
        V_lv = cmath.rect(self.V_lv_mag_kv, math.radians(self.V_lv_ang_deg))

        Z = complex(self.r_hv_ohm, self.x_hv_ohm)
        if abs(Z) < 1e-15:
            self.I_hv_in_re = 0.0
            self.I_hv_in_im = 0.0
            self.I_lv_in_re = 0.0
            self.I_lv_in_im = 0.0
            self.P_loss_mw   = 0.0
            self.Q_loss_mvar = 0.0
            return True

        y_s = 1.0 / Z                                          # S  (y_HV: HV-referred admittance)
        t   = self.rated_u1_kv / self.rated_u2_kv if self.rated_u2_kv != 0.0 else 1.0

        # Standard physical transformer model in kV/kA units:
        #   I_in_HV = -y_s·V_HV + t·y_s·V_LV
        #   I_in_LV = +t·y_s·V_HV - t²·y_s·V_LV
        I_hv = -y_s * V_hv + (y_s * t) * V_lv
        I_lv = (y_s * t) * V_hv - (y_s * t**2) * V_lv

        self.I_hv_in_re = I_hv.real
        self.I_hv_in_im = I_hv.imag
        self.I_lv_in_re = I_lv.real
        self.I_lv_in_im = I_lv.imag

        # Series current (HV-referred) for loss calculation:
        #   I_series = y_s · (V_HV - t · V_LV)
        I_series = y_s * (V_hv - t * V_lv)
        I_series_sq = abs(I_series) ** 2
        self.P_loss_mw   = I_series_sq * self.r_hv_ohm
        self.Q_loss_mvar = I_series_sq * self.x_hv_ohm

        return True

    def setup_experiment(self, start_time: float, stop_time: float = None, tolerance: float = None):
        self.time = start_time

    def exit_initialization_mode(self):
        super().exit_initialization_mode()

    def _interface(self, name: str, start: float) -> Variable:
        """Register one FMU2 interface variable."""
        _defs = {
            "r_hv_ohm":    ("Series resistance referred to HV side [Ω]",            "parameter", "fixed",      None),
            "x_hv_ohm":    ("Series reactance referred to HV side [Ω]",             "parameter", "fixed",      None),
            "rated_u1_kv": ("Rated HV voltage [kV]",                                "parameter", "fixed",      None),
            "rated_u2_kv": ("Rated LV voltage [kV]",                                "parameter", "fixed",      None),
            "V_hv_mag_kv": ("HV bus voltage magnitude [kV]",                        "input",     "continuous", None),
            "V_hv_ang_deg":("HV bus voltage angle [deg]",                           "input",     "continuous", None),
            "V_lv_mag_kv": ("LV bus voltage magnitude [kV]",                        "input",     "continuous", None),
            "V_lv_ang_deg":("LV bus voltage angle [deg]",                           "input",     "continuous", None),
            "I_hv_in_re":  ("Re(current injected into HV bus) [kA]",                "output",    "continuous", "calculated"),
            "I_hv_in_im":  ("Im(current injected into HV bus) [kA]",                "output",    "continuous", "calculated"),
            "I_lv_in_re":  ("Re(current injected into LV bus) [kA]",                "output",    "continuous", "calculated"),
            "I_lv_in_im":  ("Im(current injected into LV bus) [kA]",                "output",    "continuous", "calculated"),
            "P_loss_mw":   ("Active power loss in series impedance I²·R [MW]",      "output",    "continuous", "calculated"),
            "Q_loss_mvar": ("Reactive power loss in series impedance I²·X [MVAr]",  "output",    "continuous", "calculated"),
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
