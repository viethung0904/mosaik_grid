import math

from component_model.model import Model
from component_model.variable import Variable


class Transformer(Model):
    """CIGRE MV Network transformer FMU (Yd5, 40 MVA, 110/20 kV).

    Γ-model (CGMES convention): all series impedances referred to HV side (End 1).
    Winding connection: Yd5  —  110 kV Y-grounded / 20 kV Delta, 150° phase shift.
    Source: CIGREMV_reference_cgmes_v2_4_15_Equipment.xml

    Inputs:
        V1_mag   — voltage magnitude at HV side [V]
        V1_angle — voltage phase angle at HV side [deg]
        P2    — active power at LV side [W]
        Q2    — reactive power at LV side [VAr]

    Outputs:
        P1      — active power at HV side [W]
        Q1      — reactive power at HV side [VAr]
        V2      — voltage magnitude at LV side [V]
        dP_load — winding (copper) active losses [W]
        dQ_load — winding reactive losses [VAr]
        dQ_mag  — magnetising reactive losses [VAr]
    """

    def __init__(
        self,
        name: str = "Transformer",
        description: str = "CIGRE MV Network transformer FMU (Yd5, 40 MVA, 110/20 kV).",
        ratedS: float = 0.0,           # VA  — rated apparent power
        ratedU1: float = 0.0,          # V   — rated HV voltage (End 1)
        ratedU2: float = 0.0,          # V   — rated LV voltage (End 2)
        R: float = 0.0,                # Ω   — series resistance, positive sequence
        X: float = 0.0,                # Ω   — series reactance,  positive sequence
        B: float = 0.0,                # S   — magnetising susceptance
        G: float = 0.0,                # S   — iron-loss conductance (not given in XML)
        phaseAngleClock: int = 0,      #     — Yd5 → 150° phase shift
        V1_mag: float = 0.0,           # V   — HV voltage magnitude (input)
        V1_angle: float = 0.0,         # deg — HV voltage phase angle (input)
        P2: float = 0.0,               # W   — active power at LV side (input)
        Q2: float = 0.0,               # VAr — reactive power at LV side (input)
        P1: float = 0.0,               # W   — active power at HV side (output)
        Q1: float = 0.0,               # VAr — reactive power at HV side (output)
        V2: float = 0.0,               # V   — LV voltage magnitude (output)
        dP_load: float = 0.0,          # W   — winding active losses (output)
        dQ_load: float = 0.0,          # VAr — winding reactive losses (output)
        dQ_mag: float = 0.0,           # VAr — magnetising reactive losses (output)
        **kwargs,
    ):
        super().__init__(name, description, author="Viet Hung Pham", **kwargs)

        # Transformer parameters as FMU input variables
        self._ratedS         = self._interface("ratedS",         ratedS)
        self._ratedU1        = self._interface("ratedU1",        ratedU1)
        self._ratedU2        = self._interface("ratedU2",        ratedU2)
        self._R              = self._interface("R",              R)
        self._X              = self._interface("X",              X)
        self._B              = self._interface("B",              B)
        self._G              = self._interface("G",              G)
        self._phaseAngleClock = self._interface("phaseAngleClock", float(phaseAngleClock))

        # FMU interface variables
        self._V1_mag   = self._interface("V1_mag",   V1_mag)
        self._V1_angle = self._interface("V1_angle", V1_angle)
        self._P2       = self._interface("P2",       P2)
        self._Q2       = self._interface("Q2",       Q2)
        self._P1       = self._interface("P1",       P1)
        self._Q1       = self._interface("Q1",       Q1)
        self._V2       = self._interface("V2",       V2)
        self._dP_load  = self._interface("dP_load",  dP_load)
        self._dQ_load  = self._interface("dQ_load",  dQ_load)
        self._dQ_mag   = self._interface("dQ_mag",   dQ_mag)

        self.time = 0.0

    def do_step(self, current_time: float, step_size: float) -> bool:
        V1_mag = self.V1_mag
        V1_angle = self.V1_angle
        P2 = self.P2
        Q2 = self.Q2

        if V1_mag <= 0.0:
            self.P1 = 0.0
            self.Q1 = 0.0
            self.V2 = 0.0
            self.dP_load = 0.0
            self.dQ_load = 0.0
            self.dQ_mag = 0.0
            return True

        _a = self.ratedU1 / self.ratedU2 if self.ratedU2 != 0.0 else 1.0   # turns ratio (recomputed from live inputs)

        # ── Load (winding / copper) losses ───────────────────────────────────
        # ΔP_load = (P² + Q²) / (3·V₁²) · R
        # ΔQ_load = (P² + Q²) / (3·V₁²) · X
        S2_sq = P2**2 + Q2**2
        _k = S2_sq / (3.0 * V1_mag**2)
        self.dP_load = _k * self.R
        self.dQ_load = _k * self.X

        # ── No-load (core) losses ─────────────────────────────────────────────
        # ΔP_core = G · V₁²  (= 0 since G is not given in the XML)
        # ΔQ_mag  = |B| · V₁²
        dP_core = self.G * V1_mag**2
        self.dQ_mag = abs(self.B) * V1_mag**2

        # ── HV-side power (conservation of energy) ────────────────────────────
        self.P1 = P2 + self.dP_load + dP_core
        self.Q1 = Q2 + self.dQ_load + self.dQ_mag

        # ── LV-side voltage magnitude ─────────────────────────────────────────
        # Per-phase equivalent with complex HV phasor (line-line input):
        #   V₁_ll   = V₁_mag · e^(jθ)
        #   I       = (P₂ − jQ₂) / (√3·conj(V₁_ll))
        #   V₁_ph   = V₁_ll / √3
        #   ΔV      = Z·I
        #   V₂_ph   = V₁_ph − ΔV
        #   |V₂|    = √3 · |V₂_ph| / a
        sqrt3 = math.sqrt(3.0)
        theta = math.radians(V1_angle)
        V1_ll = complex(V1_mag * math.cos(theta), V1_mag * math.sin(theta))
        I = complex(P2, -Q2) / (sqrt3 * V1_ll.conjugate())
        Z = complex(self.R, self.X)
        V2_ph = (V1_ll / sqrt3) - (Z * I)
        self.V2 = sqrt3 * abs(V2_ph) / _a

        return True

    def setup_experiment(self, start_time: float, stop_time: float = None, tolerance: float = None):
        self.time = start_time

    def exit_initialization_mode(self):
        super().exit_initialization_mode()

    def _interface(self, name: str, start: float) -> Variable:
        """Define an FMU2 interface variable."""
        if name == "V1_mag":
            return Variable(
                self,
                name="V1_mag",
                description="Voltage magnitude at HV side (End 1) [V]",
                causality="input",
                variability="continuous",
                start=start,
                rng=(),
            )
        elif name == "V1_angle":
            return Variable(
                self,
                name="V1_angle",
                description="Voltage phase angle at HV side (End 1) [deg]",
                causality="input",
                variability="continuous",
                start=start,
                rng=(),
            )
        elif name == "P2":
            return Variable(
                self,
                name="P2",
                description="Active power at LV side (End 2) [W]",
                causality="input",
                variability="continuous",
                start=start,
                rng=(),
            )
        elif name == "Q2":
            return Variable(
                self,
                name="Q2",
                description="Reactive power at LV side (End 2) [VAr]",
                causality="input",
                variability="continuous",
                start=start,
                rng=(),
            )
        elif name == "P1":
            return Variable(
                self,
                name="P1",
                description="Active power at HV side (End 1) [W]",
                causality="output",
                variability="continuous",
                initial="calculated",
                start=start,
                rng=(),
            )
        elif name == "Q1":
            return Variable(
                self,
                name="Q1",
                description="Reactive power at HV side (End 1) [VAr]",
                causality="output",
                variability="continuous",
                initial="calculated",
                start=start,
                rng=(),
            )
        elif name == "V2":
            return Variable(
                self,
                name="V2",
                description="Voltage magnitude at LV side (End 2) [V]",
                causality="output",
                variability="continuous",
                initial="calculated",
                start=start,
                rng=(),
            )
        elif name == "dP_load":
            return Variable(
                self,
                name="dP_load",
                description="Winding (copper) active losses [W]",
                causality="output",
                variability="continuous",
                initial="calculated",
                start=start,
                rng=(),
            )
        elif name == "dQ_load":
            return Variable(
                self,
                name="dQ_load",
                description="Winding reactive losses [VAr]",
                causality="output",
                variability="continuous",
                initial="calculated",
                start=start,
                rng=(),
            )
        elif name == "dQ_mag":
            return Variable(
                self,
                name="dQ_mag",
                description="Magnetising reactive losses [VAr]",
                causality="output",
                variability="continuous",
                initial="calculated",
                start=start,
                rng=(),
            )
        elif name == "ratedS":
            return Variable(
                self,
                name="ratedS",
                description="Rated apparent power [VA]",
                causality="parameter",
                variability="fixed",
                start=start,
                rng=(),
            )
        elif name == "ratedU1":
            return Variable(
                self,
                name="ratedU1",
                description="Rated HV voltage (End 1) [V]",
                causality="parameter",
                variability="fixed",
                start=start,
                rng=(),
            )
        elif name == "ratedU2":
            return Variable(
                self,
                name="ratedU2",
                description="Rated LV voltage (End 2) [V]",
                causality="parameter",
                variability="fixed",
                start=start,
                rng=(),
            )
        elif name == "R":
            return Variable(
                self,
                name="R",
                description="Series resistance, positive sequence [Ohm]",
                causality="parameter",
                variability="fixed",
                start=start,
                rng=(),
            )
        elif name == "X":
            return Variable(
                self,
                name="X",
                description="Series reactance, positive sequence [Ohm]",
                causality="parameter",
                variability="fixed",
                start=start,
                rng=(),
            )
        elif name == "B":
            return Variable(
                self,
                name="B",
                description="Magnetising susceptance [S]",
                causality="parameter",
                variability="fixed",
                start=start,
                rng=(),
            )
        elif name == "G":
            return Variable(
                self,
                name="G",
                description="Iron-loss conductance [S]",
                causality="parameter",
                variability="fixed",
                start=start,
                rng=(),
            )
        elif name == "phaseAngleClock":
            return Variable(
                self,
                name="phaseAngleClock",
                description="Winding phase angle clock number (e.g. 5 for Yd5 = 150 deg)",
                causality="parameter",
                variability="fixed",
                start=start,
                rng=(),
            )
        else:
            raise KeyError(f"Interface variable '{name}' not defined") from None
