# pyright: reportAttributeAccessIssue=false
# pyright: reportOptionalMemberAccess=false

"""
PV Model with MPPT (Maximum Power Point Tracking)
Author: Viet Hung Pham

Enhanced PV model that integrates MPPT algorithms to dynamically track
the maximum power point under varying environmental conditions.
"""

import numpy as np
from component_model.model import Model
from component_model.variable import Variable
from scipy.optimize import fsolve


class PV_MPPT(Model):
    """PV model with integrated MPPT functionality.
    
    Includes built-in MPPT algorithms:
    - P&O (Perturb and Observe) - Default
    - IncCond (Incremental Conductance)
    - Modified_P&O (Adaptive step size)
    """

    def __init__(
        self,
        name: str = "PV_MPPT",
        description="PV system with Maximum Power Point Tracking",
        V: float = 0.0,
        I: float = 0.0,
        P: float = 0.0,
        S: float = 1000.0,
        T: float = 298.15,
        I_ph_r: float = 5.0,
        V_oc_r: float = 0.6,
        muy_I: float = 0.0005,
        muy_V: float = -0.0023,
        T_r: float = 298.15,
        S_r: float = 1000.0,
        N_s: float = 36.0,
        R_s: float = 0.5,
        R_sh: float = 100.0,
        n: float = 1.3,
        q: float = 1.60217662e-19,
        k: float = 1.38064852e-23,
        I_ph: float = 0.0,
        I_0: float = 0.0,
        mppt_algorithm: str = "P&O",
        mppt_enabled: bool = True,
        V_ref: float = 16.0,  # Initial voltage reference (close to MPP for faster startup)
        delta_V: float = 0.5,  # MPPT voltage step size (optimized for fast convergence)
        N_series_array: int = 50,  # Modules in series per string
        N_parallel_array: int = 100,  # Number of parallel strings
        **kwargs,
    ):
        super().__init__(name, description, author="Viet Hung Pham", **kwargs)
        self._V = self._interface("V", V)
        self._I = self._interface("I", I)
        self._P = self._interface("P", P)
        self._S = self._interface("S", S)
        self._T = self._interface("T", T)
        self._V_ref = self._interface("V_ref", V_ref)
        self._mppt_enabled = self._interface("mppt_enabled", 1.0 if mppt_enabled else 0.0)
        
        # PV parameters
        self.I_ph_r = I_ph_r
        self.V_oc_r = V_oc_r
        self.muy_I = muy_I
        self.muy_V = muy_V
        self.T_r = T_r
        self.S_r = S_r
        self.N_s = N_s
        self.R_s = R_s
        self.R_sh = R_sh
        self.n = n
        self.q = q
        self.k = k
        self.I_ph = I_ph
        self.I_0 = I_0
        self.time = 0.0
        
        # MPPT algorithm settings
        self.mppt_algorithm_name = mppt_algorithm
        self.delta_V = delta_V  # Voltage step size
        
        # Array configuration (series-parallel)
        self.N_series_array = N_series_array  # Modules per string
        self.N_parallel_array = N_parallel_array  # Number of strings
        
        # MPPT state variables
        self.V_prev = None
        self.I_prev = None
        self.P_prev = None
        self.mppt_initialized = False
        self.mppt_direction = 1  # 1 for increasing, -1 for decreasing
        self.adaptive_delta_V = delta_V  # For Modified P&O
        
        # Voltage limits
        V_oc_estimated = self.N_s * self.V_oc_r
        self.V_min = 0.0
        self.V_max = V_oc_estimated * 1.2
        
        # Performance tracking
        self.power_history = []

    def mppt_perturb_and_observe(self, V_current: float, I_current: float) -> float:
        """
        Perturb and Observe MPPT Algorithm
        
        Logic:
        - If power increases when voltage increases → increase voltage
        - If power decreases → reverse direction
        """
        P_current = V_current * I_current
        
        # Initialize on first call
        if not self.mppt_initialized:
            self.V_prev = V_current
            self.P_prev = P_current
            self.mppt_initialized = True
            return min(V_current + self.delta_V, self.V_max)
        
        # Calculate power and voltage changes
        delta_P = P_current - self.P_prev
        delta_V_actual = V_current - self.V_prev
        
        # P&O decision logic
        if delta_P > 0:
            # Power increased
            if delta_V_actual > 0:
                V_ref = V_current + self.delta_V  # Keep increasing
            else:
                V_ref = V_current - self.delta_V  # Keep decreasing
        else:
            # Power decreased, reverse direction
            if delta_V_actual > 0:
                V_ref = V_current - self.delta_V  # Reverse to decrease
            else:
                V_ref = V_current + self.delta_V  # Reverse to increase
        
        # Update previous values
        self.V_prev = V_current
        self.P_prev = P_current
        
        # Limit voltage to valid range
        V_ref = np.clip(V_ref, self.V_min, self.V_max)
        
        return float(V_ref)

    def mppt_incremental_conductance(self, V_current: float, I_current: float) -> float:
        """
        Incremental Conductance MPPT Algorithm
        
        At MPP: dP/dV = 0, which means: dI/dV = -I/V
        Logic:
        - dI/dV > -I/V → left of MPP → increase voltage
        - dI/dV < -I/V → right of MPP → decrease voltage
        - dI/dV ≈ -I/V → at MPP → maintain voltage
        """
        # Initialize on first call
        if not self.mppt_initialized:
            self.V_prev = V_current
            self.I_prev = I_current
            self.mppt_initialized = True
            return min(V_current + self.delta_V, self.V_max)
        
        # Calculate incremental and instantaneous conductance
        delta_I = I_current - self.I_prev
        delta_V_actual = V_current - self.V_prev
        
        # Avoid division by zero
        if abs(delta_V_actual) < 1e-6:
            V_ref = V_current  # No voltage change, maintain
        else:
            dI_dV = delta_I / delta_V_actual  # Incremental conductance
            
            if abs(V_current) < 1e-6:
                V_ref = V_current + self.delta_V  # Near zero, increase
            else:
                conductance = -I_current / V_current  # Instantaneous conductance
                tolerance = 0.01
                
                # IncCond decision logic
                if abs(dI_dV - conductance) < tolerance:
                    V_ref = V_current  # At MPP
                elif dI_dV > conductance:
                    V_ref = V_current + self.delta_V  # Left of MPP
                else:
                    V_ref = V_current - self.delta_V  # Right of MPP
        
        # Update previous values
        self.V_prev = V_current
        self.I_prev = I_current
        
        # Limit voltage
        V_ref = np.clip(V_ref, self.V_min, self.V_max)
        
        return float(V_ref)

    def mppt_modified_po(self, V_current: float, I_current: float) -> float:
        """
        Modified P&O with Adaptive Step Size
        
        Features:
        - Larger steps when far from MPP (faster convergence)
        - Smaller steps when near MPP (less oscillation)
        """
        P_current = V_current * I_current
        
        # Initialize
        if not self.mppt_initialized:
            self.V_prev = V_current
            self.P_prev = P_current
            self.adaptive_delta_V = self.delta_V * 2  # Start with larger step
            self.mppt_initialized = True
            return min(V_current + self.adaptive_delta_V, self.V_max)
        
        # Calculate power change
        delta_P = P_current - self.P_prev
        delta_V_actual = V_current - self.V_prev
        
        # Adaptive step size based on power change
        if abs(delta_P) > 0.5:  # Far from MPP
            self.adaptive_delta_V = min(self.adaptive_delta_V / 0.9, self.delta_V * 2)
        else:  # Near MPP
            self.adaptive_delta_V = max(self.adaptive_delta_V * 0.9, self.delta_V * 0.5)
        
        # P&O decision logic
        if delta_P > 0:
            if delta_V_actual > 0:
                self.mppt_direction = 1
            else:
                self.mppt_direction = -1
        else:
            if delta_V_actual > 0:
                self.mppt_direction = -1
            else:
                self.mppt_direction = 1
        
        V_ref = V_current + self.mppt_direction * self.adaptive_delta_V
        
        # Update previous values
        self.V_prev = V_current
        self.P_prev = P_current
        
        # Limit voltage
        V_ref = np.clip(V_ref, self.V_min, self.V_max)
        
        return float(V_ref)

    def run_mppt(self, V_current: float, I_current: float) -> float:
        """
        Execute the selected MPPT algorithm
        
        Returns:
            V_ref: Next voltage reference
        """
        if self.mppt_algorithm_name == "P&O":
            return self.mppt_perturb_and_observe(V_current, I_current)
        elif self.mppt_algorithm_name == "IncCond":
            return self.mppt_incremental_conductance(V_current, I_current)
        elif self.mppt_algorithm_name == "Modified_P&O":
            return self.mppt_modified_po(V_current, I_current)
        else:
            # Default to P&O if unknown algorithm
            return self.mppt_perturb_and_observe(V_current, I_current)

    def calculate_pv_at_voltage(self, V_target: float) -> tuple:
        """
        Calculate PV current at a specific voltage point
        
        Args:
            V_target: Target voltage [V]
            
        Returns:
            (V, I, P): Voltage, current, and power at the operating point
        """
        # Calculate photocurrent
        self.I_ph = float((self.S / self.S_r) * (self.I_ph_r + self.muy_I * (self.T - self.T_r)))
        
        # Calculate saturation current (Bug 1 & 2 fixed: removed N_s from denominator, moved -1 outside exp, applied muy_V)
        V_oc_corrected = self.V_oc_r + self.muy_V * (self.T - self.T_r)
        self.I_0 = float((self.I_ph_r + self.muy_I * (self.T - self.T_r)) / 
                        (np.exp(self.q * V_oc_corrected / (self.n * self.k * self.T)) - 1))
        
        # Define PV characteristic equation at fixed voltage
        def pv_equation(I_val):
            V_scalar = float(V_target)
            I_scalar = float(I_val) if np.isscalar(I_val) else float(I_val[0])
            
            exp_term = np.exp(float(self.q * ((V_scalar + I_scalar * self.R_s) / 
                                             (self.n * self.k * self.T * self.N_s))))
            shunt_term = (V_scalar + I_scalar * self.R_s) / self.R_sh
            return I_scalar - self.I_ph + self.I_0 * (exp_term - 1) + shunt_term
        
        # Solve for current
        I_guess = self.I_ph * 0.8
        solution = fsolve(pv_equation, I_guess)
        I_result = max(0.0, float(solution[0]))  # Ensure non-negative current
        V_result = float(V_target)
        P_result = V_result * I_result
        
        return V_result, I_result, P_result

    def find_true_mpp(self) -> tuple:
        """
        Find the true maximum power point by sweeping voltage
        (Used for efficiency calculation)
        
        Returns:
            (V_mpp, I_mpp, P_mpp): True MPP values
        """
        V_oc_estimated = self.N_s * self.V_oc_r * (1 + self.muy_V * (self.T - self.T_r))
        V_sweep = np.linspace(0, V_oc_estimated, 100)
        
        max_power = 0.0
        V_mpp, I_mpp, P_mpp = 0.0, 0.0, 0.0
        
        for V in V_sweep:
            V_calc, I_calc, P_calc = self.calculate_pv_at_voltage(V)
            if P_calc > max_power:
                max_power = P_calc
                V_mpp, I_mpp, P_mpp = V_calc, I_calc, P_calc
        
        return V_mpp, I_mpp, P_mpp

    def do_step(self, current_time: float, step_size: float) -> bool:
        """Execute one simulation step with MPPT"""
        
        if self.mppt_enabled > 0.5:  # MPPT enabled
            # First, calculate current operating point
            V_current, I_current, P_current = self.calculate_pv_at_voltage(self.V_ref)
            
            # Run MPPT algorithm to get next voltage reference
            self.V_ref = self.run_mppt(V_current, I_current)
            
            # Calculate new operating point
            self.V, self.I, self.P = self.calculate_pv_at_voltage(self.V_ref)
            
        else:  # MPPT disabled, operate at fixed V_ref
            self.V, self.I, self.P = self.calculate_pv_at_voltage(self.V_ref)
        
        # Scale for array configuration (series-parallel)
        # Series: voltages add, current unchanged
        # Parallel: currents add, voltage unchanged
        self.V = self.N_series_array * float(self.V)  # N_series strings add voltages
        self.I = self.N_parallel_array * float(self.I)  # N_parallel strings add currents
        self.P = self.V * self.I  # Power from V and I
        
        # At night (no power), set voltage to zero for physical correctness
        if abs(self.P) < 1.0:  # Power threshold (1W)
            self.V = 0.0
        
        # Track performance
        self.power_history.append(self.P)
        
        return True
    
    def setup_experiment(self, start_time: float, stop_time: float = None, tolerance: float = None):
        """Set initial (non-interface) variables."""
        self.time = start_time
        # Reset MPPT state
        self.mppt_initialized = False
        self.V_prev = None
        self.I_prev = None
        self.P_prev = None

    def exit_initialization_mode(self):
        """Initialize the model after initial variables are set."""
        super().exit_initialization_mode()

    def _interface(self, name: str, start: str | float | tuple) -> Variable:
        """Define a FMU3 interface variable."""
        if name == "I":
            return Variable(
                self,
                name="I",
                description="Current output of the PV model",
                causality="output",
                variability="continuous",
                initial="exact",
                start=start,
                rng=(),
            )
        elif name == "V":
            return Variable(
                self,
                name="V",
                description="Voltage output of the PV model",
                causality="output",
                variability="continuous",
                initial="exact",
                start=start,
                rng=(),
            )
        elif name == "P":
            return Variable(
                self,
                name="P",
                description="Power output of the PV model",
                causality="output",
                variability="continuous",
                initial="exact",
                start=start,
                rng=(),
            )
        elif name == "S":
            return Variable(
                self,
                name="S",
                description="Solar irradiance input",
                causality="input",
                variability="continuous",
                start=start,
                rng=(),
            )
        elif name == "T":
            return Variable(
                self,
                name="T",
                description="Temperature input",
                causality="input",
                variability="continuous",
                start=start,
                rng=(),
            )
        elif name == "V_ref":
            return Variable(
                self,
                name="V_ref",
                description="Voltage reference from MPPT",
                causality="output",
                variability="continuous",
                initial="exact",
                start=start,
                rng=(),
            )
        elif name == "mppt_enabled":
            return Variable(
                self,
                name="mppt_enabled",
                description="MPPT enable flag (1=enabled, 0=disabled)",
                causality="parameter",
                variability="fixed",
                start=start,
                rng=(),
            )
        else:
            raise ValueError(f"Unknown interface variable: {name}")


if __name__ == "__main__":
    """Build the FMU with MPPT"""
    asBuilt = Model.build("./PV_MPPT.py")
    print(f"✓ FMU built successfully: {asBuilt}")
