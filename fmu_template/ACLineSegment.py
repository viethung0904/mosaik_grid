import cmath
import math

from component_model.model import Model
from component_model.variable import Variable


class ACLineSegment(Model):
    """AC Line Segment FMU — LIM-compatible current-injection model.

    Computes the branch current flowing from→to given both end voltages.
    Intended to be coupled with SubstationFMU instances via a LIM master.

    LIM wiring:
        I = (V_from − V_to) / Z_series     [kA]

    The master reads I from this FMU and accumulates signed current sums
    per bus before calling each SubstationFMU.do_step().

    Parameters:
        r_ohm  — series resistance  [Ω]
        x_ohm  — series reactance   [Ω]
        bch    — total shunt susceptance  [S]  (stored for Y_self calculation,
                 not used in branch current — master adds bch/2 to Y_self)

    Inputs:
        V_from_mag_kv  — sending-end voltage magnitude   [kV]
        V_from_ang_deg — sending-end voltage angle        [deg]
        V_to_mag_kv    — receiving-end voltage magnitude  [kV]
        V_to_ang_deg   — receiving-end voltage angle      [deg]

    Outputs:
        I_from_mag_kA  — current leaving from-bus (series + shunt at from end)  [kA]
        I_from_ang_deg — angle of I_from                                        [deg]
        I_to_mag_kA    — current entering to-bus (series − shunt at to end)    [kA]
        I_to_ang_deg   — angle of I_to                                          [deg]

    Master wiring (sign convention):
        I_net_from -= cmath.rect(line.I_from_mag_kA, radians(line.I_from_ang_deg))
        I_net_to   += cmath.rect(line.I_to_mag_kA,   radians(line.I_to_ang_deg))

    Note: set B_shunt=0 in all connected Substation FMUs — shunt is fully
    modelled here and must not be double-counted.

    Outputs (losses):
        P_loss_mw   — active power loss in series impedance  I²·R  [MW]
        Q_loss_mvar — reactive power loss in series impedance I²·X  [MVAr]
    """

    def __init__(
        self,
        name: str = "ACLineSegment",
        description: str = "AC Line Segment FMU — LIM current-injection model.",
        r_ohm: float = 1.0,
        x_ohm: float = 1.0,
        bch: float = 0.0,
        V_from_mag_kv: float = 0.0,
        V_from_ang_deg: float = 0.0,
        V_to_mag_kv: float = 0.0,
        V_to_ang_deg: float = 0.0,
        I_from_mag_kA: float = 0.0,
        I_from_ang_deg: float = 0.0,
        I_from_neg_ang_deg: float = 0.0,
        I_to_mag_kA: float = 0.0,
        I_to_ang_deg: float = 0.0,
        P_loss_mw: float = 0.0,
        Q_loss_mvar: float = 0.0,
        **kwargs,
    ):
        super().__init__(name, description, author="Viet Hung Pham", **kwargs)

        # Parameters
        self._r_ohm         = self._interface("r_ohm",         r_ohm)
        self._x_ohm         = self._interface("x_ohm",         x_ohm)
        self._bch           = self._interface("bch",           bch)

        # Inputs
        self._V_from_mag_kv  = self._interface("V_from_mag_kv",  V_from_mag_kv)
        self._V_from_ang_deg = self._interface("V_from_ang_deg", V_from_ang_deg)
        self._V_to_mag_kv    = self._interface("V_to_mag_kv",    V_to_mag_kv)
        self._V_to_ang_deg   = self._interface("V_to_ang_deg",   V_to_ang_deg)

        # Outputs
        self._I_from_mag_kA      = self._interface("I_from_mag_kA",      I_from_mag_kA)
        self._I_from_ang_deg     = self._interface("I_from_ang_deg",     I_from_ang_deg)
        self._I_from_neg_ang_deg = self._interface("I_from_neg_ang_deg", I_from_neg_ang_deg)
        self._I_to_mag_kA        = self._interface("I_to_mag_kA",        I_to_mag_kA)
        self._I_to_ang_deg   = self._interface("I_to_ang_deg",   I_to_ang_deg)
        self._P_loss_mw      = self._interface("P_loss_mw",      P_loss_mw)
        self._Q_loss_mvar    = self._interface("Q_loss_mvar",     Q_loss_mvar)

        self.time = 0.0

    def do_step(self, current_time: float, step_size: float) -> bool:
        """Compute pi-model branch currents  [kA].

        Pi-model:
            I_series     = (V_from − V_to) / Z          series current
            I_shunt_from = j·(bch/2)·V_from             shunt at from end (to ground)
            I_shunt_to   = j·(bch/2)·V_to               shunt at to end (to ground)

            I_from = I_series + I_shunt_from   (leaves from-bus into line)
            I_to   = I_series − I_shunt_to     (arrives at to-bus from line)

        Units: kV / kΩ = kA;  S · kV = kA  (both consistent).
        """
        V_from = cmath.rect(self.V_from_mag_kv, math.radians(self.V_from_ang_deg))
        V_to   = cmath.rect(self.V_to_mag_kv,   math.radians(self.V_to_ang_deg))

        Z      = complex(self.r_ohm, self.x_ohm)              # Ω; kV/Ω = kA ✓
        Y_half = complex(0.0, self.bch / 2.0)               # S  (S·kV = kA ✓)

        I_series = (V_from - V_to) / Z if abs(Z) > 1e-15 else complex(0.0, 0.0)
        I_shunt_from = Y_half * V_from
        I_shunt_to   = Y_half * V_to

        I_from = I_series + I_shunt_from
        I_to   = I_series - I_shunt_to

        self.I_from_mag_kA      = abs(I_from)
        self.I_from_ang_deg     = math.degrees(cmath.phase(I_from))
        self.I_from_neg_ang_deg = math.degrees(cmath.phase(-I_from))
        self.I_to_mag_kA        = abs(I_to)
        self.I_to_ang_deg   = math.degrees(cmath.phase(I_to))

        # Series loss: P + jQ = |I_series|² · Z*   (kA² · Ω = MW)
        I_series_sq = abs(I_series) ** 2
        self.P_loss_mw   = I_series_sq * self.r_ohm
        self.Q_loss_mvar = I_series_sq * self.x_ohm
        return True

    def setup_experiment(self, start_time: float, stop_time: float = None, tolerance: float = None):
        self.time = start_time

    def exit_initialization_mode(self):
        super().exit_initialization_mode()

    def _interface(self, name: str, start: float) -> Variable:
        """Register one FMU2 interface variable."""
        _defs = {
            "r_ohm":         ("Series resistance [Ω]",                           "parameter", "fixed",      None),
            "x_ohm":         ("Series reactance [Ω]",                            "parameter", "fixed",      None),
            "bch":           ("Total shunt susceptance [S]",                     "parameter", "fixed",      None),
            "V_from_mag_kv": ("Sending-end voltage magnitude [kV]",              "input",     "continuous", None),
            "V_from_ang_deg":("Sending-end voltage angle [deg]",                 "input",     "continuous", None),
            "V_to_mag_kv":   ("Receiving-end voltage magnitude [kV]",            "input",     "continuous", None),
            "V_to_ang_deg":  ("Receiving-end voltage angle [deg]",               "input",     "continuous", None),
            "I_from_mag_kA":  ("Current leaving from-bus into line (series + shunt_from) [kA]", "output", "continuous", "calculated"),
            "I_from_ang_deg":     ("Angle of I_from [deg]",                                          "output", "continuous", "calculated"),
            "I_from_neg_ang_deg": ("Angle of −I_from [deg] — for from-bus signed subtraction",     "output", "continuous", "calculated"),
            "I_to_mag_kA":        ("Current entering to-bus from line (series − shunt_to) [kA]",    "output", "continuous", "calculated"),
            "I_to_ang_deg":   ("Angle of I_to [deg]",                                            "output", "continuous", "calculated"),
            "P_loss_mw":      ("Active power loss in series impedance I²·R [MW]",                "output", "continuous", "calculated"),
            "Q_loss_mvar":    ("Reactive power loss in series impedance I²·X [MVAr]",            "output", "continuous", "calculated"),
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
