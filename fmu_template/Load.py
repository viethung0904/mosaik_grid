from component_model.model import Model
from component_model.variable import Variable


class Load(Model):
    """Constant power load FMU.

    Outputs fixed active and reactive power demand.
    Designed to connect to SubstationFMU inputs P_load_mw / Q_load_mvar.

    Parameters:
        p_mw   — active power demand  [MW]
        q_mvar — reactive power demand [MVAr]

    Outputs:
        P_load_mw   — active power demand  [MW]
        Q_load_mvar — reactive power demand [MVAr]
    """

    def __init__(
        self,
        name: str = "Load",
        description: str = "Constant power load FMU.",
        p_mw: float = 0.0,
        q_mvar: float = 0.0,
        P_load_mw: float = 0.0,
        Q_load_mvar: float = 0.0,
        **kwargs,
    ):
        super().__init__(name, description, author="Viet Hung Pham", **kwargs)

        # Parameters
        self._p_mw    = self._interface("p_mw",    p_mw)
        self._q_mvar  = self._interface("q_mvar",  q_mvar)

        # Outputs
        self._P_load_mw   = self._interface("P_load_mw",   P_load_mw)
        self._Q_load_mvar = self._interface("Q_load_mvar", Q_load_mvar)

        self.time = 0.0

    def do_step(self, current_time: float, step_size: float) -> bool:
        """Output the fixed load values every step."""
        self.P_load_mw   = self.p_mw
        self.Q_load_mvar = self.q_mvar
        return True

    def setup_experiment(self, start_time: float, stop_time: float = None, tolerance: float = None):
        self.time = start_time
        self.P_load_mw   = self.p_mw
        self.Q_load_mvar = self.q_mvar

    def exit_initialization_mode(self):
        super().exit_initialization_mode()

    def _interface(self, name: str, start: float) -> Variable:
        """Register one FMU2 interface variable."""
        _defs = {
            "p_mw":        ("Active power demand parameter [MW]",    "parameter", "fixed",      None),
            "q_mvar":      ("Reactive power demand parameter [MVAr]","parameter", "fixed",      None),
            "P_load_mw":   ("Active power demand output [MW]",       "output",    "continuous", "exact"),
            "Q_load_mvar": ("Reactive power demand output [MVAr]",   "output",    "continuous", "exact"),
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
