# pyright: reportAttributeAccessIssue=false
# pyright: reportOptionalMemberAccess=false
"""
IEEE_14_NR_PV_Battery_unified.py — Monolithic IEEE 14-bus co-simulation FMU.

All physics solved inside a SINGLE do_step() call — no mosaik, no inter-FMU
communication, no time_shifted connections needed.

Equations solved per physical time step (dt = 600 s, 10 min):
  A. PV array  — single-diode I-V model + Perturb-and-Observe MPPT
                 (identical equations to PV_Python_fmi2.fmu / PV_MPPT.py)
  B. Battery   — Thevenin equivalent (V_oc(SOC), constant-P charge strategy)
                 simplified model: battery FMU (Battery_Simulink_fmi3.fmu) is a
                 Simulink binary with unreadable internals; physics re-implemented
                 from first principles to match its observable I/O behaviour.
  C. Network   — Polar Newton-Raphson power flow, up to N_NR = 5 inner iterations.
                 Mirrors N_LIM = 5 outer GJ sweeps in the mosaik scenario.
                 Full Y-bus (pi-model lines + off-nominal-tap transformers).
  D. Losses    — AC line pi-model active/reactive power losses.

time_shifted connections are NOT required here: all voltages live in the shared
numpy array self._Vc; there is no algebraic loop between separate FMU instances.

FMI 2.0 Co-simulation interface
────────────────────────────────
Inputs (continuous, updated every doStep):
    S   [W/m²]  solar irradiance → PV photocurrent
    T   [K]     PV cell temperature

Parameters (fixed at instantiation):
    p_charge_mw     battery charge setpoint [MW]  (>0 = charging from grid)
    pv_scale_factor PV output multiplier   (10.0 → peak ≈ 3.84 MW for 50S×100P)

Outputs (continuous):
    V_BUS{n}_{kv}_mag_kv  — bus voltage magnitude [kV]  for each of 14 buses
    V_BUS{n}_{kv}_ang_deg — bus voltage angle     [deg] for each of 14 buses
    Bat_SOC       [%]     state of charge (0–100)
    Bat_V_volt    [V]     terminal voltage
    Bat_I_amp     [A]     terminal current (>0 = charging)
    Bat_P_mw      [MW]    grid power drawn (>0 = charging load on grid)
    PV_P_W        [W]     array output power (before scale_factor)
    PV_V_volt     [V]     array terminal voltage
    PV_I_amp      [A]     array current
    PV_P_mw       [MW]    grid power injected (<0 = generation into grid)
    Total_P_loss_mw   [MW]   total active network losses
    Total_Q_loss_mvar [MVAr] total reactive network losses

Building the FMU
────────────────
  cd /home/hungpv/mosaik_grid/fmu_template
  /home/hungpv/miniforge3/envs/mosaik/bin/python3 \\
      -c "from component_model.model import Model; \\
          Model.build('./IEEE_14_NR_PV_Battery_unified.py')"
  cp IEEE_14_NR_PV_Battery_unified.fmu ../fmus/

Running without mosaik (FMPy driver)
──────────────────────────────────────
  See src/run_unified_fmu.py
"""

import cmath
import math
import os

import numpy as np
from dotenv import load_dotenv
from neo4j import GraphDatabase

from component_model.model import Model
from component_model.variable import Variable


# ── Module-level helpers ──────────────────────────────────────────────────────

def _bus_attr(bus_name: str) -> str:
    """Map Neo4j bus name to FMI output variable name prefix.

    Examples
    --------
    'BUS_1   69.0'  → 'V_BUS1_69'
    'BUS_10  13.8'  → 'V_BUS10_138'
    'BUS_8   18.0'  → 'V_BUS8_18'
    """
    parts = bus_name.strip().split()          # ['BUS_10', '13.8']
    num   = parts[0].replace('BUS_', '')      # '10'
    v_tag = parts[1].replace('.', '')         # '138' / '690' / '180'
    while v_tag.endswith('0') and len(v_tag) > 1:
        v_tag = v_tag[:-1]                    # '690'→'69', '180'→'18', keep '138'
    return f'V_BUS{num}_{v_tag}'              # 'V_BUS10_138'


# ── Unified FMU class ─────────────────────────────────────────────────────────

class IEEE_14_NR_PV_Battery_Unified(Model):
    """IEEE 14-bus unified power-flow FMU — NR + PV (MPPT) + Battery."""

    # ── FMI variable registry: name → (description, causality, variability, initial)
    # Used by the overridden _interface() to declare every FMI scalar correctly.
    _VAR_DEFS: dict = {
        # ── Inputs ────────────────────────────────────────────────────────────
        "S":                   ("Solar irradiance [W/m²]",                 "input",     "continuous", None),
        "T":                   ("PV cell temperature [K]",                 "input",     "continuous", None),
        # ── Parameters ────────────────────────────────────────────────────────
        "p_charge_mw":         ("Battery charge setpoint [MW] (>0=charge)","parameter", "fixed",      None),
        "pv_scale_factor":     ("PV output multiplier",                    "parameter", "fixed",      None),
        # ── Bus voltage outputs — 14 buses (IEEE-standard numbering) ──────────
        "V_BUS1_69_mag_kv":    ("|V| BUS_1  69.0 kV [kV]",  "output","continuous","calculated"),
        "V_BUS1_69_ang_deg":   ("∠  BUS_1  69.0 kV [deg]",  "output","continuous","calculated"),
        "V_BUS2_69_mag_kv":    ("|V| BUS_2  69.0 kV [kV]",  "output","continuous","calculated"),
        "V_BUS2_69_ang_deg":   ("∠  BUS_2  69.0 kV [deg]",  "output","continuous","calculated"),
        "V_BUS3_69_mag_kv":    ("|V| BUS_3  69.0 kV [kV]",  "output","continuous","calculated"),
        "V_BUS3_69_ang_deg":   ("∠  BUS_3  69.0 kV [deg]",  "output","continuous","calculated"),
        "V_BUS4_69_mag_kv":    ("|V| BUS_4  69.0 kV [kV]",  "output","continuous","calculated"),
        "V_BUS4_69_ang_deg":   ("∠  BUS_4  69.0 kV [deg]",  "output","continuous","calculated"),
        "V_BUS5_69_mag_kv":    ("|V| BUS_5  69.0 kV [kV]",  "output","continuous","calculated"),
        "V_BUS5_69_ang_deg":   ("∠  BUS_5  69.0 kV [deg]",  "output","continuous","calculated"),
        "V_BUS6_138_mag_kv":   ("|V| BUS_6  13.8 kV [kV]",  "output","continuous","calculated"),
        "V_BUS6_138_ang_deg":  ("∠  BUS_6  13.8 kV [deg]",  "output","continuous","calculated"),
        "V_BUS7_138_mag_kv":   ("|V| BUS_7  13.8 kV [kV]",  "output","continuous","calculated"),
        "V_BUS7_138_ang_deg":  ("∠  BUS_7  13.8 kV [deg]",  "output","continuous","calculated"),
        "V_BUS8_18_mag_kv":    ("|V| BUS_8  18.0 kV [kV]",  "output","continuous","calculated"),
        "V_BUS8_18_ang_deg":   ("∠  BUS_8  18.0 kV [deg]",  "output","continuous","calculated"),
        "V_BUS9_138_mag_kv":   ("|V| BUS_9  13.8 kV [kV]",  "output","continuous","calculated"),
        "V_BUS9_138_ang_deg":  ("∠  BUS_9  13.8 kV [deg]",  "output","continuous","calculated"),
        "V_BUS10_138_mag_kv":  ("|V| BUS_10 13.8 kV [kV]",  "output","continuous","calculated"),
        "V_BUS10_138_ang_deg": ("∠  BUS_10 13.8 kV [deg]",  "output","continuous","calculated"),
        "V_BUS11_138_mag_kv":  ("|V| BUS_11 13.8 kV [kV]",  "output","continuous","calculated"),
        "V_BUS11_138_ang_deg": ("∠  BUS_11 13.8 kV [deg]",  "output","continuous","calculated"),
        "V_BUS12_138_mag_kv":  ("|V| BUS_12 13.8 kV [kV]",  "output","continuous","calculated"),
        "V_BUS12_138_ang_deg": ("∠  BUS_12 13.8 kV [deg]",  "output","continuous","calculated"),
        "V_BUS13_138_mag_kv":  ("|V| BUS_13 13.8 kV [kV]",  "output","continuous","calculated"),
        "V_BUS13_138_ang_deg": ("∠  BUS_13 13.8 kV [deg]",  "output","continuous","calculated"),
        "V_BUS14_138_mag_kv":  ("|V| BUS_14 13.8 kV [kV]",  "output","continuous","calculated"),
        "V_BUS14_138_ang_deg": ("∠  BUS_14 13.8 kV [deg]",  "output","continuous","calculated"),
        # ── Battery outputs ───────────────────────────────────────────────────
        "Bat_SOC":             ("Battery state of charge [%]",             "output","continuous","calculated"),
        "Bat_V_volt":          ("Battery terminal voltage [V]",            "output","continuous","calculated"),
        "Bat_I_amp":           ("Battery terminal current [A] (>0=charge)","output","continuous","calculated"),
        "Bat_P_mw":            ("Battery grid power [MW] (>0=charge draw)","output","continuous","calculated"),
        # ── PV outputs ────────────────────────────────────────────────────────
        "PV_P_W":              ("PV array power [W]",                      "output","continuous","calculated"),
        "PV_V_volt":           ("PV array voltage [V]",                    "output","continuous","calculated"),
        "PV_I_amp":            ("PV array current [A]",                    "output","continuous","calculated"),
        "PV_P_mw":             ("PV grid power [MW] (<0=generation)",      "output","continuous","calculated"),
        # ── Network aggregate losses ──────────────────────────────────────────
        "Total_P_loss_mw":     ("Total active network losses [MW]",        "output","continuous","calculated"),
        "Total_Q_loss_mvar":   ("Total reactive network losses [MVAr]",    "output","continuous","calculated"),
    }

    # ── Mosaik-compatible output key lists (IEEE 14-bus topology) ─────────────
    # Note: dots replaced with underscores so component_model can parse names.
    # run_unified_fmu.py restores dots when writing output_whole_system.json.
    _LINE_KEYS = [
        'line_bus_1___69_0_bus_2___69_0', 'line_bus_1___69_0_bus_5___69_0',
        'line_bus_2___69_0_bus_3___69_0', 'line_bus_2___69_0_bus_4___69_0',
        'line_bus_2___69_0_bus_5___69_0', 'line_bus_3___69_0_bus_4___69_0',
        'line_bus_4___69_0_bus_5___69_0',
        'line_bus_6___13_8_bus_11__13_8', 'line_bus_6___13_8_bus_12__13_8',
        'line_bus_6___13_8_bus_13__13_8', 'line_bus_7___13_8_bus_9___13_8',
        'line_bus_9___13_8_bus_10__13_8', 'line_bus_9___13_8_bus_14__13_8',
        'line_bus_10__13_8_bus_11__13_8', 'line_bus_12__13_8_bus_13__13_8',
        'line_bus_13__13_8_bus_14__13_8',
    ]
    _BRANCH_KEYS = ['branch_8', 'branch_9', 'branch_10', 'branch_14']

    # Extend _VAR_DEFS with mosaik-compatible per-element outputs
    for _lk in _LINE_KEYS:
        _VAR_DEFS[f'P_loss_{_lk}_mw']   = (f"P_loss {_lk} [MW]",   "output","continuous","calculated")
        _VAR_DEFS[f'Q_loss_{_lk}_mvar'] = (f"Q_loss {_lk} [MVAr]", "output","continuous","calculated")
        _VAR_DEFS[f'I_from_{_lk}_kA']   = (f"I_from {_lk} [kA]",   "output","continuous","calculated")
        _VAR_DEFS[f'I_to_{_lk}_kA']     = (f"I_to {_lk} [kA]",     "output","continuous","calculated")
    for _bk in _BRANCH_KEYS:
        _VAR_DEFS[f'P_loss_{_bk}_mw']   = (f"P_loss {_bk} [MW]",   "output","continuous","calculated")
        _VAR_DEFS[f'Q_loss_{_bk}_mvar'] = (f"Q_loss {_bk} [MVAr]", "output","continuous","calculated")
    del _lk, _bk

    def __init__(
        self,
        name: str = "IEEE14_NR_PV_Battery_Unified",
        description: str = "IEEE 14-bus unified NR power flow + PV (MPPT) + Battery",
        # ── FMI inputs ────────────────────────────────────────────────────────
        S: float = 1000.0,               # W/m²  solar irradiance
        T: float = 298.15,               # K     PV cell temperature
        # ── FMI parameters ────────────────────────────────────────────────────
        p_charge_mw: float = 0.030,      # MW    battery charge setpoint
        pv_scale_factor: float = 10.0,   #       PV output multiplier
        # ── FMI bus voltage outputs (14 buses) ────────────────────────────────
        V_BUS1_69_mag_kv: float = 0.0,   V_BUS1_69_ang_deg: float = 0.0,
        V_BUS2_69_mag_kv: float = 0.0,   V_BUS2_69_ang_deg: float = 0.0,
        V_BUS3_69_mag_kv: float = 0.0,   V_BUS3_69_ang_deg: float = 0.0,
        V_BUS4_69_mag_kv: float = 0.0,   V_BUS4_69_ang_deg: float = 0.0,
        V_BUS5_69_mag_kv: float = 0.0,   V_BUS5_69_ang_deg: float = 0.0,
        V_BUS6_138_mag_kv: float = 0.0,  V_BUS6_138_ang_deg: float = 0.0,
        V_BUS7_138_mag_kv: float = 0.0,  V_BUS7_138_ang_deg: float = 0.0,
        V_BUS8_18_mag_kv: float = 0.0,   V_BUS8_18_ang_deg: float = 0.0,
        V_BUS9_138_mag_kv: float = 0.0,  V_BUS9_138_ang_deg: float = 0.0,
        V_BUS10_138_mag_kv: float = 0.0, V_BUS10_138_ang_deg: float = 0.0,
        V_BUS11_138_mag_kv: float = 0.0, V_BUS11_138_ang_deg: float = 0.0,
        V_BUS12_138_mag_kv: float = 0.0, V_BUS12_138_ang_deg: float = 0.0,
        V_BUS13_138_mag_kv: float = 0.0, V_BUS13_138_ang_deg: float = 0.0,
        V_BUS14_138_mag_kv: float = 0.0, V_BUS14_138_ang_deg: float = 0.0,
        # ── FMI battery outputs ───────────────────────────────────────────────
        Bat_SOC: float = 50.0,
        Bat_V_volt: float = 500.0,
        Bat_I_amp: float = 0.0,
        Bat_P_mw: float = 0.0,
        # ── FMI PV outputs ────────────────────────────────────────────────────
        PV_P_W: float = 0.0,
        PV_V_volt: float = 0.0,
        PV_I_amp: float = 0.0,
        PV_P_mw: float = 0.0,
        # ── FMI network aggregate outputs ─────────────────────────────────────
        Total_P_loss_mw: float = 0.0,
        Total_Q_loss_mvar: float = 0.0,
        **kwargs,
    ):
        super().__init__(name, description, author="Viet Hung Pham", **kwargs)

        # ── Register FMI variables (causality declared in _VAR_DEFS) ──────────
        self._S                   = self._interface("S",                   S)
        self._T                   = self._interface("T",                   T)
        self._p_charge_mw         = self._interface("p_charge_mw",         p_charge_mw)
        self._pv_scale_factor     = self._interface("pv_scale_factor",     pv_scale_factor)

        self._V_BUS1_69_mag_kv    = self._interface("V_BUS1_69_mag_kv",    V_BUS1_69_mag_kv)
        self._V_BUS1_69_ang_deg   = self._interface("V_BUS1_69_ang_deg",   V_BUS1_69_ang_deg)
        self._V_BUS2_69_mag_kv    = self._interface("V_BUS2_69_mag_kv",    V_BUS2_69_mag_kv)
        self._V_BUS2_69_ang_deg   = self._interface("V_BUS2_69_ang_deg",   V_BUS2_69_ang_deg)
        self._V_BUS3_69_mag_kv    = self._interface("V_BUS3_69_mag_kv",    V_BUS3_69_mag_kv)
        self._V_BUS3_69_ang_deg   = self._interface("V_BUS3_69_ang_deg",   V_BUS3_69_ang_deg)
        self._V_BUS4_69_mag_kv    = self._interface("V_BUS4_69_mag_kv",    V_BUS4_69_mag_kv)
        self._V_BUS4_69_ang_deg   = self._interface("V_BUS4_69_ang_deg",   V_BUS4_69_ang_deg)
        self._V_BUS5_69_mag_kv    = self._interface("V_BUS5_69_mag_kv",    V_BUS5_69_mag_kv)
        self._V_BUS5_69_ang_deg   = self._interface("V_BUS5_69_ang_deg",   V_BUS5_69_ang_deg)
        self._V_BUS6_138_mag_kv   = self._interface("V_BUS6_138_mag_kv",   V_BUS6_138_mag_kv)
        self._V_BUS6_138_ang_deg  = self._interface("V_BUS6_138_ang_deg",  V_BUS6_138_ang_deg)
        self._V_BUS7_138_mag_kv   = self._interface("V_BUS7_138_mag_kv",   V_BUS7_138_mag_kv)
        self._V_BUS7_138_ang_deg  = self._interface("V_BUS7_138_ang_deg",  V_BUS7_138_ang_deg)
        self._V_BUS8_18_mag_kv    = self._interface("V_BUS8_18_mag_kv",    V_BUS8_18_mag_kv)
        self._V_BUS8_18_ang_deg   = self._interface("V_BUS8_18_ang_deg",   V_BUS8_18_ang_deg)
        self._V_BUS9_138_mag_kv   = self._interface("V_BUS9_138_mag_kv",   V_BUS9_138_mag_kv)
        self._V_BUS9_138_ang_deg  = self._interface("V_BUS9_138_ang_deg",  V_BUS9_138_ang_deg)
        self._V_BUS10_138_mag_kv  = self._interface("V_BUS10_138_mag_kv",  V_BUS10_138_mag_kv)
        self._V_BUS10_138_ang_deg = self._interface("V_BUS10_138_ang_deg", V_BUS10_138_ang_deg)
        self._V_BUS11_138_mag_kv  = self._interface("V_BUS11_138_mag_kv",  V_BUS11_138_mag_kv)
        self._V_BUS11_138_ang_deg = self._interface("V_BUS11_138_ang_deg", V_BUS11_138_ang_deg)
        self._V_BUS12_138_mag_kv  = self._interface("V_BUS12_138_mag_kv",  V_BUS12_138_mag_kv)
        self._V_BUS12_138_ang_deg = self._interface("V_BUS12_138_ang_deg", V_BUS12_138_ang_deg)
        self._V_BUS13_138_mag_kv  = self._interface("V_BUS13_138_mag_kv",  V_BUS13_138_mag_kv)
        self._V_BUS13_138_ang_deg = self._interface("V_BUS13_138_ang_deg", V_BUS13_138_ang_deg)
        self._V_BUS14_138_mag_kv  = self._interface("V_BUS14_138_mag_kv",  V_BUS14_138_mag_kv)
        self._V_BUS14_138_ang_deg = self._interface("V_BUS14_138_ang_deg", V_BUS14_138_ang_deg)

        self._Bat_SOC             = self._interface("Bat_SOC",             Bat_SOC)
        self._Bat_V_volt          = self._interface("Bat_V_volt",          Bat_V_volt)
        self._Bat_I_amp           = self._interface("Bat_I_amp",           Bat_I_amp)
        self._Bat_P_mw            = self._interface("Bat_P_mw",            Bat_P_mw)

        self._PV_P_W              = self._interface("PV_P_W",              PV_P_W)
        self._PV_V_volt           = self._interface("PV_V_volt",           PV_V_volt)
        self._PV_I_amp            = self._interface("PV_I_amp",            PV_I_amp)
        self._PV_P_mw             = self._interface("PV_P_mw",             PV_P_mw)

        self._Total_P_loss_mw     = self._interface("Total_P_loss_mw",     Total_P_loss_mw)
        self._Total_Q_loss_mvar   = self._interface("Total_Q_loss_mvar",   Total_Q_loss_mvar)

        # ── Register mosaik-compatible per-element and alias outputs ──────────
        for _lk in self._LINE_KEYS:
            self._interface(f'P_loss_{_lk}_mw',   0.0)
            self._interface(f'Q_loss_{_lk}_mvar', 0.0)
            self._interface(f'I_from_{_lk}_kA',   0.0)
            self._interface(f'I_to_{_lk}_kA',     0.0)
        for _bk in self._BRANCH_KEYS:
            self._interface(f'P_loss_{_bk}_mw',   0.0)
            self._interface(f'Q_loss_{_bk}_mvar', 0.0)

        # ── Internal state placeholders (populated in _load_network) ──────────
        # NOTE: Neo4j queries are deferred to setup_experiment() so that
        # component_model.Model.build() can instantiate this class without a live
        # database connection (the builder only needs the FMI variable declarations).
        self._network_loaded: bool = False
        self._Vc:             np.ndarray | None = None
        self._Y:              np.ndarray | None = None
        self._bus_names:      list = []
        self._bus_idx:        dict = {}
        self._ns_idx:         np.ndarray = np.array([], dtype=int)
        self._pq_idx:         np.ndarray = np.array([], dtype=int)
        self._sl_idx:         np.ndarray = np.array([], dtype=int)
        self._ns_names:       list = []
        self._pq_names:       list = []
        self._pv_idx_nr:      np.ndarray = np.array([], dtype=int)
        self._V_reg:          dict = {}
        self._V_slack:        np.ndarray = np.array([], dtype=complex)
        self._P_inj_base:     np.ndarray = np.array([], dtype=float)
        self._Q_inj_base:     np.ndarray = np.array([], dtype=float)
        self._lines:                 list = []
        self._bus_output_map:        dict = {}
        self._pv_bus_idx:     int | None = None
        self._bat_bus_idx:    int | None = None
        self._all_slack_set:  set = set()
        self._pq_set:         set = set()
        self._pv_reg_set:     set = set()

        self.time = 0.0

        # ── PV MPPT state (P&O algorithm) ─────────────────────────────────────
        # Single-diode module parameters (identical to PV_Python_fmi2.fmu defaults)
        self._pv_I_ph_r  = 5.0           # A    reference photocurrent per module
        self._pv_V_oc_r  = 0.6           # V    reference open-circuit voltage per cell
        self._pv_muy_I   = 0.0005        # A/K  temperature coefficient for I_ph
        self._pv_muy_V   = -0.0023       # V/K  temperature coefficient for V_oc
        self._pv_T_r     = 298.15        # K    reference temperature
        self._pv_S_r     = 1000.0        # W/m² reference irradiance
        self._pv_N_s     = 36            #      cells per module
        self._pv_R_s     = 0.5           # Ω    series resistance per module
        self._pv_R_sh    = 100.0         # Ω    shunt resistance per module
        self._pv_n       = 1.3           #      diode ideality factor
        self._pv_q       = 1.60217662e-19  # C  electron charge
        self._pv_k       = 1.38064852e-23  # J/K Boltzmann constant
        self._pv_Ns_arr  = 50            #      modules in series per string
        self._pv_Np_arr  = 100           #      parallel strings

        # Module-level open-circuit voltage (initialise V_ref near MPP)
        self._pv_V_oc_module = self._pv_N_s * self._pv_V_oc_r  # ≈ 21.6 V
        self._pv_V_ref       = 16.0                              # start near MPP — matches PV_Python_fmi2.fmu V_ref start
        self._pv_delta_V     = 0.5           # V  MPPT perturbation step per call
        self._pv_V_prev: float | None = None
        self._pv_P_prev: float | None = None
        self._pv_mppt_init   = False

        # ── Battery state (Thevenin equivalent) ───────────────────────────────
        # Parameters calibrated by running Battery_Simulink_fmi3.fmu at open-circuit
        # and two charge levels to extract V_oc(SOC), R_int, and capacity:
        #   V_oc = 4.0 × SOC_pct  (linear, 0 V at 0%, 400 V at 100%)
        #   R_int from ΔV/ΔI at constant SOC: (360.0222−360.0)/41.6641 = 0.000533 Ω
        #   Capacity from SOC rate at 30 kW: 0.5 kWh / 0.0222% = 2252 kWh → 6256 Ah
        # Sign convention matches Simulink FMU: I < 0 = charging, I > 0 = discharging.
        self._bat_soc_pct   = 90.0         # %   initial SOC — matches Battery_Simulink_fmi3.fmu
        self._bat_cap_ah    = 12530.0      # Ah  usable capacity (calibrated vs Simulink FMU:
                                           #     observed ΔSOC 0.011%/step at 83 A, 60 s →
                                           #     C = 83.3 × (60/3600) / 0.000111 = 12 530 Ah)
        self._bat_V_oc_min  = 0.0          # V   OCV at SOC = 0%  (calibrated)
        self._bat_V_oc_max  = 400.0        # V   OCV at SOC = 100% (calibrated)
        self._bat_R_int     = 0.000533     # Ω   internal resistance (calibrated)

        # Initialise FMI outputs from initial state
        self._sync_outputs()

    # ── FMI variable registration ─────────────────────────────────────────────

    def _interface(self, name: str, start: float) -> Variable:
        """Declare one FMI scalar variable with explicit causality."""
        if name not in self._VAR_DEFS:
            raise KeyError(
                f"[IEEE14_Unified] Unknown interface variable: '{name}'. "
                "Add it to _VAR_DEFS first."
            ) from None
        desc, causality, variability, initial = self._VAR_DEFS[name]
        kwargs: dict = dict(
            name=name,
            description=desc,
            causality=causality,
            variability=variability,
            start=start,
            rng=(),
        )
        if initial is not None:
            kwargs['initial'] = initial
        return Variable(self, **kwargs)

    # ── FMI lifecycle ─────────────────────────────────────────────────────────

    def _load_network(self) -> None:
        """Query Neo4j, build Y-bus and set up all NR solver data structures.

        Called once from setup_experiment() the first time the FMU is used.
        Identical queries and construction logic as standalone_pf.py.
        """
        load_dotenv()   # searches for .env from cwd upward
        uri  = os.getenv('NEO4J_URI')
        user = os.getenv('NEO4J_USERNAME')
        pwd  = os.getenv('NEO4J_PASSWORD')
        db   = os.getenv('NEO4J_DATABASE')
        if not all([uri, user, pwd, db]):
            raise RuntimeError(
                'IEEE14_NR_PV_Battery_Unified: Neo4j environment variables not set. '
                'Ensure .env contains NEO4J_URI, NEO4J_USERNAME, NEO4J_PASSWORD, NEO4J_DATABASE.'
            )

        driver = GraphDatabase.driver(uri, auth=(user, pwd))
        try:
            with driver.session(database=db) as sess:
                sub_records = sess.run(
                    'MATCH (s:Substation) '
                    'RETURN s.name AS name, s.nominal_voltage_kv AS v_nom, '
                    '       s.is_slack AS is_slack, s.is_sync_machine AS is_sync_machine, '
                    '       s.sv_voltage_kv AS sv_v, s.sv_angle_deg AS sv_ang, '
                    '       s.p_gen_mw AS p_gen_mw, s.q_gen_mvar AS q_gen_mvar '
                    'ORDER BY s.name'
                ).data()
                subs = {
                    r['name']: {
                        'v_nom':    r['v_nom']     or 20.0,
                        'is_slack': bool(r['is_slack']) if r['is_slack'] is not None else False,
                        'is_sync':  bool(r['is_sync_machine']) if r['is_sync_machine'] is not None else False,
                        'sv_v':     r['sv_v']      or r['v_nom'] or 20.0,
                        'sv_ang':   r['sv_ang']    or 0.0,
                        'p_gen':    r['p_gen_mw']  or 0.0,
                        'q_gen':    r['q_gen_mvar'] or 0.0,
                    }
                    for r in sub_records
                }

                line_records = sess.run(
                    'MATCH (a:Substation)-[l:LINE]->(b:Substation) '
                    'RETURN a.name AS from_sub, b.name AS to_sub, '
                    '       l.r_ohm AS r_ohm, l.x_ohm AS x_ohm, l.bch AS bch '
                    'ORDER BY a.name, b.name'
                ).data()

                tr_records = sess.run(
                    'MATCH (t:Transformer) RETURN t ORDER BY t.name'
                ).data()
                transformers = {r['t']['name']: dict(r['t']) for r in tr_records}

                tr_edge_records = sess.run(
                    'MATCH (hv:Substation)-[:CONNECT_TO {side:"HV"}]->(t:Transformer)'
                    '-[:CONNECT_TO {side:"LV"}]->(lv:Substation) '
                    'RETURN hv.name AS hv_sub, t.name AS tr_name, lv.name AS lv_sub'
                ).data()

                load_records = sess.run(
                    'MATCH (s:Substation)-[:CONNECT_TO]->(l:Load) '
                    'RETURN s.name AS sub_name, l.p_mw AS p_mw, l.q_mvar AS q_mvar'
                ).data()
                loads: dict = {}
                for r in load_records:
                    bn = r['sub_name']
                    loads[bn] = {
                        'p_mw':   (loads.get(bn, {}).get('p_mw',   0.0) or 0.0) + (r['p_mw']   or 0.0),
                        'q_mvar': (loads.get(bn, {}).get('q_mvar', 0.0) or 0.0) + (r['q_mvar'] or 0.0),
                    }

                pv_rec = sess.run(
                    'MATCH (s:Substation)-[:CONNECT_TO]->(d:PV) '
                    'RETURN s.name AS name ORDER BY s.name LIMIT 1'
                ).single()
                bat_rec = sess.run(
                    'MATCH (s:Substation)-[:CONNECT_TO]->(d:Battery) '
                    'RETURN s.name AS name ORDER BY s.name LIMIT 1'
                ).single()
        finally:
            driver.close()

        # ── Bus ordering & type classification ────────────────────────────────
        self._bus_names  = sorted(subs.keys())
        n_bus            = len(self._bus_names)
        self._bus_idx    = {nm: i for i, nm in enumerate(self._bus_names)}

        slack_primary    = {nm for nm, s in subs.items() if s['is_slack']}
        sync_machines    = {nm for nm, s in subs.items() if s['is_sync']}
        lv_slack         = {
            e['lv_sub'] for e in tr_edge_records
            if e['lv_sub'] not in slack_primary
            and not subs.get(e['lv_sub'], {}).get('is_sync', False)
        }
        all_slack        = slack_primary | lv_slack

        pv_buses_nr      = sorted([nm for nm in sync_machines if nm not in all_slack])
        pq_buses_nr      = sorted([nm for nm in self._bus_names
                                   if nm not in all_slack and nm not in set(pv_buses_nr)])
        non_slack        = [nm for nm in self._bus_names if nm not in all_slack]

        self._ns_idx     = np.array([self._bus_idx[nm] for nm in non_slack],   dtype=int)
        self._pq_idx     = np.array([self._bus_idx[nm] for nm in pq_buses_nr], dtype=int)
        self._pv_idx_nr  = np.array([self._bus_idx[nm] for nm in pv_buses_nr], dtype=int)
        self._sl_idx     = np.array([self._bus_idx[nm] for nm in all_slack],   dtype=int)
        self._ns_names   = non_slack
        self._pq_names   = pq_buses_nr
        self._V_reg      = {nm: (subs[nm]['sv_v'] or subs[nm]['v_nom']) for nm in pv_buses_nr}

        # ── Build full Y-bus (n × n, complex) ─────────────────────────────────
        Y = np.zeros((n_bus, n_bus), dtype=complex)
        self._lines = []
        self._trafos = []   # for loss calculation
        for l in line_records:
            fi  = self._bus_idx[l['from_sub']]
            ti  = self._bus_idx[l['to_sub']]
            Z   = complex(l['r_ohm'] or 0.0, l['x_ohm'] or 0.0)
            y_s = (1.0 / Z) if abs(Z) > 1e-15 else complex(1e6, 0)
            y_sh = complex(0, (l['bch'] or 0.0) / 2.0)
            Y[fi, fi] += y_s + y_sh
            Y[ti, ti] += y_s + y_sh
            Y[fi, ti] -= y_s
            Y[ti, fi] -= y_s
            bch_val = l['bch'] or 0.0
            _lv_key = (f"line_{l['from_sub'].lower().replace('-','_').replace(' ','_').replace('.','_')}"
                       f"_{l['to_sub'].lower().replace('-','_').replace(' ','_').replace('.','_')}")
            self._lines.append({
                'fi': fi, 'ti': ti,
                'r': l['r_ohm'] or 0.0, 'x': l['x_ohm'] or 0.0,
                'bch': bch_val,
                'y_s': y_s,
                'mosaik_lv': _lv_key if _lv_key in self._LINE_KEYS else None,
                'label': f"{l['from_sub']}→{l['to_sub']}",
            })

        # Transformers: off-nominal tap model (HV-referred impedance)
        # Matches TransformerBranch.py: I_in_HV = -y_s·V_HV + t·y_s·V_LV
        #                               I_in_LV = +t·y_s·V_HV − t²·y_s·V_LV
        # → Y[HV,HV] += y_s,  Y[LV,LV] += t²·y_s,  Y[HV,LV] = Y[LV,HV] = −t·y_s
        for e in tr_edge_records:
            hv_nm, lv_nm, tr_nm = e['hv_sub'], e['lv_sub'], e['tr_name']
            t_data = transformers.get(tr_nm, {})
            x_hv = t_data.get('hv_x_ohm') or 0.0
            x_lv = t_data.get('lv_x_ohm') or 0.0
            r_hv = t_data.get('hv_r_ohm') or 0.0
            r_lv = t_data.get('lv_r_ohm') or 0.0
            u1   = (t_data.get('hv_rated_u_kv') or t_data.get('hv_nominal_voltage_kv')
                    or subs[hv_nm]['v_nom'])
            u2   = (t_data.get('lv_rated_u_kv') or t_data.get('lv_nominal_voltage_kv')
                    or subs[lv_nm]['v_nom'])
            if abs(x_hv) < 1e-12 and abs(x_lv) > 1e-12:
                x_hv = x_lv * (u1 / u2) ** 2
                r_hv = r_lv * (u1 / u2) ** 2
            Z_hv = complex(r_hv, x_hv)
            tap  = (u1 / u2) if u2 != 0.0 else 1.0
            y_s  = (1.0 / Z_hv) if abs(Z_hv) > 1e-15 else complex(1e6, 0)
            hi, li = self._bus_idx[hv_nm], self._bus_idx[lv_nm]
            Y[hi, hi] += y_s
            Y[li, li] += y_s * tap ** 2
            Y[hi, li] -= y_s * tap
            Y[li, hi] -= y_s * tap
            # Detect inverted HV/LV storage (Neo4j CONNECT_TO sides swapped).
            # scenario_adaptive.py corrects this with a v_nom check; replicate here.
            inverted = subs[hv_nm]['v_nom'] < subs[lv_nm]['v_nom']
            _tv_key = tr_nm.lower().replace('-', '_')
            self._trafos.append({
                'hi': hi, 'li': li,
                'r': r_hv, 'x': x_hv, 'tap': tap,
                'inverted': inverted,
                'mosaik_tv': _tv_key if _tv_key in self._BRANCH_KEYS else None,
                'label': f"{hv_nm}→{lv_nm}",
            })

        self._Y = Y

        # ── Scheduled base power injections ───────────────────────────────────
        self._P_inj_base = np.zeros(n_bus, dtype=float)
        self._Q_inj_base = np.zeros(n_bus, dtype=float)
        for nm, s in subs.items():
            i = self._bus_idx[nm]
            p_ld = loads.get(nm, {}).get('p_mw',   0.0)
            q_ld = loads.get(nm, {}).get('q_mvar', 0.0)
            self._P_inj_base[i] = s['p_gen'] - p_ld
            self._Q_inj_base[i] = s['q_gen'] - q_ld

        # ── Bus → FMI output variable name mapping ────────────────────────────
        self._bus_output_map = {}
        for nm in self._bus_names:
            attr = _bus_attr(nm)
            if f'{attr}_mag_kv' in self._VAR_DEFS:
                self._bus_output_map[nm] = attr

        # ── PV / Battery bus indices ──────────────────────────────────────────
        # PV and Battery nodes may not exist in the DB; fall back to scenario defaults.
        _pv_nm  = pv_rec['name']  if pv_rec  else 'BUS_14  13.8'
        _bat_nm = bat_rec['name'] if bat_rec else 'BUS_5   69.0'
        self._pv_bus_idx  = self._bus_idx.get(_pv_nm)
        self._bat_bus_idx = self._bus_idx.get(_bat_nm)
        print(f'  [Unified FMU] PV bus:  {_pv_nm}  (idx={self._pv_bus_idx})')
        print(f'  [Unified FMU] Bat bus: {_bat_nm}  (idx={self._bat_bus_idx})')

        # ── Sets for fast per-bus type lookup ────────────────────────────────
        self._all_slack_set = all_slack           # set of slack bus names
        self._pq_set        = set(pq_buses_nr)    # set of PQ bus names
        self._pv_reg_set    = set(pv_buses_nr)    # set of PV (voltage-regulated) bus names

        # ── Warm-start voltage state from CIM SvVoltage reference ─────────────
        self._Vc = np.array(
            [cmath.rect(subs[nm]['sv_v'], math.radians(subs[nm]['sv_ang']))
             for nm in self._bus_names],
            dtype=complex,
        )
        self._V_slack = np.array(
            [cmath.rect(subs[nm]['sv_v'], math.radians(subs[nm]['sv_ang']))
             if nm in all_slack else 0j
             for nm in self._bus_names],
            dtype=complex,
        )

        self._network_loaded = True
        print(f'  [Unified FMU] Network loaded: {n_bus} buses, {len(self._lines)} lines')

    def setup_experiment(self, start_time: float, stop_time: float = None,
                         tolerance: float = None):
        self.time = start_time
        if not self._network_loaded:
            self._load_network()
        # Reset MPPT state so it re-initialises on first do_step
        self._pv_mppt_init = False
        self._pv_V_prev    = None
        self._pv_P_prev    = None

    def exit_initialization_mode(self):
        super().exit_initialization_mode()
        if self._network_loaded:
            self._sync_outputs()

    # ── Main simulation step ──────────────────────────────────────────────────

    def do_step(self, current_time: float, step_size: float) -> bool:
        """One physical time step — integrates all three sub-models.

        step_size = 600 s (10 min) when driven by FMPy driver.

        The inner N_NR = 5 Newton-Raphson iterations replace the N_LIM = 5
        mosaik outer-loop ticks in scenario_IEEE_14_NR.py.  No time_shifted
        connections are needed because bus voltages are a shared numpy array.

        Convergence criterion: max |ΔP|, |ΔQ| < 1 × 10⁻⁶ MW / MVAr.
        Warm-start: voltages from the previous step → NR typically converges
        in 2–3 iterations.
        """
        N_NR = 5          # outer GJ sweeps (mirrors N_LIM in mosaik scenario)

        # ── A. PV array (single-diode + P&O MPPT) ────────────────────────────
        # Step 1: evaluate I-V at current V_ref (measurement for P&O decision)
        pv_V_mod, pv_I_mod, pv_P_mod = self._pv_calc_iv_module(
            self.V_ref_module, self.S, self.T
        )
        # Step 2: P&O perturbs V_ref immediately this step (matches PV_MPPT.py do_step)
        self.V_ref_module = self._pv_mppt_po(pv_V_mod, pv_P_mod)
        # Step 3: re-evaluate at the new V_ref so output is post-perturbation
        pv_V_mod, pv_I_mod, pv_P_mod = self._pv_calc_iv_module(
            self.V_ref_module, self.S, self.T
        )
        # Scale to array (N_series × N_parallel)
        pv_V_arr = self._pv_Ns_arr * pv_V_mod
        pv_I_arr = self._pv_Np_arr * pv_I_mod
        pv_P_arr = pv_V_arr * pv_I_arr          # W (total array output)
        # Grid convention (mirrors pv_simulator.py):  P_load_mw = -P*scale/1e6
        pv_P_grid_mw = -pv_P_arr * self.pv_scale_factor / 1e6  # MW (<0 = injection)

        # ── B. Battery (Thevenin SOC dynamics) ────────────────────────────────
        bat_P_mw, bat_soc, bat_V, bat_I = self._battery_step(
            self.p_charge_mw, step_size
        )

        # ── C. Build per-step nodal injection vector ──────────────────────────
        P_inj = self._P_inj_base.copy()
        Q_inj = self._Q_inj_base.copy()
        # PV: generation → positive net injection (reduces load)
        if self._pv_bus_idx is not None:
            P_inj[self._pv_bus_idx] -= pv_P_grid_mw   # pv_P_grid_mw < 0 → P_inj increases
        # Battery: charging → extra load → negative net injection
        if self._bat_bus_idx is not None:
            P_inj[self._bat_bus_idx] -= bat_P_mw       # bat_P_mw > 0 charging → P_inj decreases

        # ── D. SubstationNR-style Gauss-Jacobi outer loop ──────────────────────
        # N_NR outer GJ sweeps, each solving a per-bus rectangular NR to convergence.
        # Mirrors the mosaik co-simulation exactly:
        #   • SubstationNR per-bus local NR  ↔  this inner rectangular solve
        #   • N_LIM=5 mosaik GJ ticks        ↔  N_NR=5 outer sweeps here
        #   • omega_relax=0.5 damping        ↔  omega below
        #   • time_shifted line FMU outputs  ↔  I_nbr uses Vc from previous sweep
        NR_MAX_ITER = 50
        NR_TOL      = 1e-10   # [kA] convergence tolerance on |f(V)|
        omega       = 0.5     # outer Gauss-Jacobi damping (same as Substation FMU)

        Vc = self._Vc.copy()
        # Enforce slack voltages exactly before first sweep
        for si in self._sl_idx:
            Vc[si] = self._V_slack[si]

        for _outer in range(N_NR):
            Vc_next = Vc.copy()

            for nm in self._bus_names:
                i = self._bus_idx[nm]

                # Slack buses: hold at reference
                if nm in self._all_slack_set:
                    Vc_next[i] = self._V_slack[i]
                    continue

                # Neighbor current (Gauss-Jacobi: uses Vc from previous outer sweep)
                # I_nbr = Σ_{j≠i} y_ij * V_j = Y[i,i]*V_i - (Y@V)[i]
                I_nbr = self._Y[i, i] * Vc[i] - complex(self._Y[i] @ Vc)

                P     = P_inj[i]   # net injection: P_gen − P_load  (positive = generation)
                Q     = Q_inj[i]
                Y_re  = self._Y[i, i].real
                Y_im  = self._Y[i, i].imag   # includes shunt susceptance

                if nm in self._pv_reg_set:
                    # ── PV bus: analytical angle solve at fixed |V| = V_reg ──────
                    # Real-power balance: P_inj = V_reg²·Y_re − V_reg·(I_re·cosθ + I_im·sinθ)
                    # → I_re·cosθ + I_im·sinθ = (V_reg²·Y_re − P_inj) / V_reg = K
                    V_reg = self._V_reg[nm]
                    K     = (V_reg * V_reg * Y_re - P) / V_reg
                    I_mag = abs(I_nbr)
                    if I_mag < 1e-15:
                        V_NR = cmath.rect(V_reg, cmath.phase(Vc[i]))
                    else:
                        ratio   = max(-1.0, min(1.0, K / I_mag))
                        phi_I   = cmath.phase(I_nbr)
                        delta   = math.acos(ratio)
                        theta_1 = phi_I + delta
                        theta_2 = phi_I - delta
                        theta_old = cmath.phase(Vc[i])
                        if abs(theta_1 - theta_old) <= abs(theta_2 - theta_old):
                            theta = theta_1
                        else:
                            theta = theta_2
                        V_NR = cmath.rect(V_reg, theta)

                else:
                    # ── PQ bus: rectangular Newton-Raphson ────────────────────
                    # Solves: f(V) = I_nbr − Y[i,i]·V + conj(S_inj/V) = 0
                    # Injection convention: f_re = I_re − Y_re·x + Y_im·y + A/D
                    #                      f_im = I_im − Y_re·y − Y_im·x + C/D
                    # where A = P·x + Q·y,  C = P·y − Q·x,  D = x²+y²
                    x, y = Vc[i].real, Vc[i].imag
                    for _nr in range(NR_MAX_ITER):
                        D = x * x + y * y
                        if D < 1e-18:
                            break
                        A = P * x + Q * y
                        C = P * y - Q * x
                        f_re = I_nbr.real - Y_re * x + Y_im * y + A / D
                        f_im = I_nbr.imag - Y_re * y - Y_im * x + C / D
                        if math.hypot(f_re, f_im) < NR_TOL:
                            break
                        D2  = D * D
                        J11 = (P * D - 2.0 * x * A) / D2 - Y_re
                        J12 = (Q * D - 2.0 * y * A) / D2 + Y_im
                        J21 = -(Q * D + 2.0 * x * C) / D2 - Y_im
                        J22 = (P * D - 2.0 * y * C) / D2 - Y_re
                        det = J11 * J22 - J12 * J21
                        if abs(det) < 1e-30:
                            break
                        x += -(J22 * f_re - J12 * f_im) / det
                        y +=  (J21 * f_re - J11 * f_im) / det
                    V_NR = complex(x, y)

                # Outer Gauss-Jacobi relaxation (mirrors omega_relax=0.5 in SubstationNR)
                Vc_next[i] = Vc[i] + omega * (V_NR - Vc[i])
                # PV buses: pin magnitude at regulation setpoint after relaxation
                if nm in self._pv_reg_set:
                    Vc_next[i] = cmath.rect(self._V_reg[nm], cmath.phase(Vc_next[i]))

            # Re-enforce slack at end of each outer sweep
            for si in self._sl_idx:
                Vc_next[si] = self._V_slack[si]

            Vc = Vc_next

        self._Vc = Vc   # store for warm-start next step

        # ── E. Compute AC line pi-model losses ───────────────────────────────
        total_P_loss, total_Q_loss = self._compute_losses()

        # ── F. Write FMI output variables ────────────────────────────────────
        self._sync_bus_outputs()
        self.Bat_SOC           = bat_soc
        self.Bat_V_volt        = bat_V
        self.Bat_I_amp         = bat_I
        self.Bat_P_mw          = bat_P_mw
        self.PV_P_W            = pv_P_arr
        self.PV_V_volt         = pv_V_arr if pv_P_arr > 1.0 else 0.0
        self.PV_I_amp          = pv_I_arr
        self.PV_P_mw           = pv_P_grid_mw
        self.Total_P_loss_mw   = total_P_loss
        self.Total_Q_loss_mvar = total_Q_loss

        self.time = current_time + step_size
        return True

    # ── PV sub-model ─────────────────────────────────────────────────────────

    @property
    def V_ref_module(self) -> float:
        return self._pv_V_ref

    @V_ref_module.setter
    def V_ref_module(self, v: float) -> None:
        V_oc_max = self._pv_N_s * (self._pv_V_oc_r + self._pv_muy_V * (self.T - self._pv_T_r))
        self._pv_V_ref = float(np.clip(v, 0.0, V_oc_max * 1.2))

    def _pv_calc_iv_module(self, V_target: float, S: float, T: float
                            ) -> tuple[float, float, float]:
        """Single-diode I-V equation solved via Newton-Raphson (per module).

        Returns (V, I, P) at the given operating voltage for one module.
        Identical equations to calculate_pv_at_voltage() in PV_MPPT.py.
        """
        if S < 0.1:
            return V_target, 0.0, 0.0   # night — no photocurrent

        I_ph_ref = self._pv_I_ph_r + self._pv_muy_I * (T - self._pv_T_r)
        I_ph     = (S / self._pv_S_r) * I_ph_ref
        V_oc_corrected = self._pv_V_oc_r + self._pv_muy_V * (T - self._pv_T_r)
        # I_0 uses reference I_ph (irradiance-independent) — matches PV_MPPT.py exactly
        denom = math.exp(self._pv_q * V_oc_corrected /
                         (self._pv_n * self._pv_k * T)) - 1.0
        I_0 = I_ph_ref / denom if abs(denom) > 1e-30 else 1e-12

        # Thermal voltage factor: q / (n * N_s * k * T)
        a = self._pv_q / (self._pv_n * self._pv_N_s * self._pv_k * T)

        # Newton-Raphson: f(I) = I - I_ph + I_0*(exp(a*(V+I*Rs))-1) + (V+I*Rs)/Rsh = 0
        # f'(I) = 1 + I_0*a*Rs*exp(a*(V+I*Rs)) + Rs/Rsh
        I_sol = I_ph * 0.9   # initial guess near short-circuit current
        for _ in range(50):
            VpIR   = V_target + I_sol * self._pv_R_s
            exp_arg = min(a * VpIR, 700.0)
            exp_val = math.exp(exp_arg)
            f  = I_sol - I_ph + I_0 * (exp_val - 1.0) + VpIR / self._pv_R_sh
            df = 1.0 + I_0 * a * self._pv_R_s * exp_val + self._pv_R_s / self._pv_R_sh
            step = f / df
            I_sol -= step
            I_sol  = max(0.0, I_sol)   # keep current non-negative
            if abs(step) < 1e-9:
                break

        I_sol = max(0.0, I_sol)
        return float(V_target), float(I_sol), float(V_target * I_sol)

    def _pv_mppt_po(self, V_current: float, P_current: float) -> float:
        """Perturb-and-Observe MPPT: decide next module voltage reference.

        Identical logic to mppt_perturb_and_observe() in PV_MPPT.py.
        """
        if not self._pv_mppt_init:
            self._pv_V_prev    = V_current
            self._pv_P_prev    = P_current
            self._pv_mppt_init = True
            return min(V_current + self._pv_delta_V, self._pv_V_oc_module * 1.2)

        delta_P = P_current - self._pv_P_prev
        delta_V = V_current - self._pv_V_prev

        if delta_P > 0:
            V_ref = V_current + self._pv_delta_V if delta_V > 0 else V_current - self._pv_delta_V
        else:
            V_ref = V_current - self._pv_delta_V if delta_V > 0 else V_current + self._pv_delta_V

        self._pv_V_prev = V_current
        self._pv_P_prev = P_current
        V_oc_max = self._pv_N_s * (self._pv_V_oc_r + self._pv_muy_V * (self.T - self._pv_T_r))
        return float(np.clip(V_ref, 0.0, V_oc_max * 1.2))

    # ── Battery sub-model ─────────────────────────────────────────────────────

    def _battery_step(self, p_charge_setpoint_mw: float, dt_s: float
                       ) -> tuple[float, float, float, float]:
        """Thevenin battery — SOC ODE + terminal voltage.

        Returns (P_grid_mw, SOC_pct, V_terminal, I_bat).

        Sign conventions (match Battery_Simulink_fmi3.fmu):
            P_grid_mw > 0   →  charging   (drawing power from grid)
            P_grid_mw < 0   →  discharging (injecting power into grid)
            I_bat < 0       →  charging current  (Simulink convention)
            I_bat > 0       →  discharging current

        Thevenin model (calibrated to Simulink FMU):
            V_oc = V_oc_min + (SOC/100) × (V_oc_max − V_oc_min)
            V_terminal = V_oc + R_int × I_internal
              where I_internal > 0 for charging (current into + terminal)
              → charging raises V_terminal above V_oc
              → discharging lowers V_terminal below V_oc

        SOC ODE (forward Euler, I_internal > 0 = charging → SOC rises):
            dSOC_pct = (I_internal / C_ah) × (dt_s / 3600) × 100
        """
        soc_frac = self._bat_soc_pct / 100.0

        # BMS limits
        if soc_frac >= 1.0 and p_charge_setpoint_mw > 0:
            return 0.0, 100.0, self._bat_V_oc_max, 0.0
        if soc_frac <= 0.0 and p_charge_setpoint_mw < 0:
            return 0.0, 0.0, self._bat_V_oc_min, 0.0

        V_oc = self._bat_V_oc_min + soc_frac * (self._bat_V_oc_max - self._bat_V_oc_min)

        # I_internal > 0 = charging (current into + terminal)
        # p_charge_setpoint_mw > 0 → P_bat_W > 0 → I_internal > 0
        P_bat_W  = p_charge_setpoint_mw * 1e6
        I_int    = P_bat_W / V_oc if abs(V_oc) > 1e-3 else 0.0
        # Charging: V_terminal = V_oc + R_int * I_int  (terminal rises above OCV)
        V_terminal = V_oc + self._bat_R_int * I_int
        I_int_ref  = P_bat_W / V_terminal if abs(V_terminal) > 1e-3 else I_int
        V_terminal = V_oc + self._bat_R_int * I_int_ref

        # SOC update
        delta_soc_pct = (I_int_ref / self._bat_cap_ah) * (dt_s / 3600.0) * 100.0
        self._bat_soc_pct = float(np.clip(self._bat_soc_pct + delta_soc_pct, 0.0, 100.0))

        # Output I uses Simulink sign: negative = charging
        return (p_charge_setpoint_mw,
                self._bat_soc_pct,
                float(V_terminal),
                -float(I_int_ref))

    # ── NR Jacobian ───────────────────────────────────────────────────────────

    def _build_jacobian(self, Vc: np.ndarray,
                        P_calc: np.ndarray,
                        Q_calc: np.ndarray) -> np.ndarray:
        """Build the standard polar power-flow Jacobian [H N; J L].

        Rows/cols: non-slack buses for dP/dθ (H, N),
                   PQ buses for dQ/d|V| (J, L).

        Identical formulation to standalone_pf.py.
        """
        n_ns = len(self._ns_idx)
        n_pq = len(self._pq_idx)
        J    = np.zeros((n_ns + n_pq, n_ns + n_pq), dtype=float)

        for ri, r_name in enumerate(self._ns_names):
            ir   = self._bus_idx[r_name]
            Vr   = Vc[ir]; Vr_m = abs(Vr); θr = cmath.phase(Vr)

            # ── H block: dP_r / dθ_c ─────────────────────────────────────────
            for ci, c_name in enumerate(self._ns_names):
                ic   = self._bus_idx[c_name]
                Vc_  = Vc[ic]; Vc_m = abs(Vc_); θc = cmath.phase(Vc_)
                G    = self._Y[ir, ic].real; B = self._Y[ir, ic].imag
                dθ   = θr - θc
                if ri == ci:
                    J[ri, ci] = -Q_calc[ir] - B * Vr_m ** 2
                else:
                    J[ri, ci] = -Vr_m * Vc_m * (G * math.sin(dθ) - B * math.cos(dθ))

            # ── N block: dP_r / d|V_c| ───────────────────────────────────────
            for ci, c_name in enumerate(self._pq_names):
                ic   = self._bus_idx[c_name]
                Vc_  = Vc[ic]; Vc_m = abs(Vc_); θc = cmath.phase(Vc_)
                G    = self._Y[ir, ic].real; B = self._Y[ir, ic].imag
                dθ   = θr - θc
                if r_name == c_name:
                    J[ri, n_ns + ci] = P_calc[ir] / Vr_m + G * Vr_m
                else:
                    J[ri, n_ns + ci] = Vr_m * (G * math.cos(dθ) + B * math.sin(dθ))

        for ri, r_name in enumerate(self._pq_names):
            ir   = self._bus_idx[r_name]
            Vr   = Vc[ir]; Vr_m = abs(Vr); θr = cmath.phase(Vr)

            # ── J block: dQ_r / dθ_c ─────────────────────────────────────────
            for ci, c_name in enumerate(self._ns_names):
                ic   = self._bus_idx[c_name]
                Vc_  = Vc[ic]; Vc_m = abs(Vc_); θc = cmath.phase(Vc_)
                G    = self._Y[ir, ic].real; B = self._Y[ir, ic].imag
                dθ   = θr - θc
                if r_name == c_name:
                    J[n_ns + ri, ci] = P_calc[ir] - G * Vr_m ** 2
                else:
                    J[n_ns + ri, ci] = Vr_m * Vc_m * (G * math.cos(dθ) + B * math.sin(dθ))

            # ── L block: dQ_r / d|V_c| ───────────────────────────────────────
            for ci, c_name in enumerate(self._pq_names):
                ic   = self._bus_idx[c_name]
                Vc_  = Vc[ic]; Vc_m = abs(Vc_); θc = cmath.phase(Vc_)
                G    = self._Y[ir, ic].real; B = self._Y[ir, ic].imag
                dθ   = θr - θc
                if r_name == c_name:
                    J[n_ns + ri, n_ns + ci] = Q_calc[ir] / Vr_m - B * Vr_m
                else:
                    J[n_ns + ri, n_ns + ci] = Vr_m * (G * math.sin(dθ) - B * math.cos(dθ))

        return J

    # ── Loss calculation ──────────────────────────────────────────────────────

    def _compute_losses(self) -> tuple[float, float]:
        """Series I²R and I²X losses; also sets per-element mosaik FMI outputs.

        Returns (total_P_loss_mw, total_Q_loss_mvar).
        """
        total_P = 0.0
        total_Q = 0.0
        for l in self._lines:
            V_from = self._Vc[l['fi']]
            V_to   = self._Vc[l['ti']]
            if abs(l['y_s']) < 1e-30:
                continue
            Y_half   = complex(0.0, l['bch'] / 2.0)
            I_series = (V_from - V_to) * l['y_s']     # kV × S = kA
            I_from   = I_series + Y_half * V_from
            I_to     = I_series - Y_half * V_to
            I_sq     = abs(I_series) ** 2              # kA²
            P_loss   = I_sq * l['r']                   # kA² × Ω = MW
            Q_loss   = I_sq * l['x']
            total_P += P_loss
            total_Q += Q_loss
            lk = l.get('mosaik_lv')
            if lk:
                setattr(self, f'P_loss_{lk}_mw',   P_loss)
                setattr(self, f'Q_loss_{lk}_mvar', Q_loss)
                setattr(self, f'I_from_{lk}_kA',   abs(I_from))
                setattr(self, f'I_to_{lk}_kA',     abs(I_to))
        for t in self._trafos:
            # For transformers where Neo4j has HV/LV sides swapped (v_nom check),
            # the actual HV bus is stored as li and LV as hi — swap for I_series.
            if t.get('inverted'):
                V_hv, V_lv = self._Vc[t['li']], self._Vc[t['hi']]
            else:
                V_hv, V_lv = self._Vc[t['hi']], self._Vc[t['li']]
            Z    = complex(t['r'], t['x'])
            if abs(Z) < 1e-15:
                continue
            I_series = (V_hv - t['tap'] * V_lv) / Z   # HV-referred series current [kA]
            I_sq     = abs(I_series) ** 2
            P_loss   = I_sq * t['r']
            Q_loss   = I_sq * t['x']
            total_P += P_loss
            total_Q += Q_loss
            tv = t.get('mosaik_tv')
            if tv:
                setattr(self, f'P_loss_{tv}_mw',   P_loss)
                setattr(self, f'Q_loss_{tv}_mvar', Q_loss)
        return total_P, total_Q

    # ── Output synchronisation ────────────────────────────────────────────────

    def _sync_bus_outputs(self):
        """Write converged bus voltages to all registered FMI output variables."""
        for nm, attr_prefix in self._bus_output_map.items():
            i    = self._bus_idx[nm]
            Vc_i = self._Vc[i]
            setattr(self, f'{attr_prefix}_mag_kv', abs(Vc_i))
            setattr(self, f'{attr_prefix}_ang_deg', math.degrees(cmath.phase(Vc_i)))

    def _sync_outputs(self):
        """Write all FMI outputs from current internal state (called at init)."""
        self._sync_bus_outputs()
        soc_frac   = self._bat_soc_pct / 100.0
        V_oc       = self._bat_V_oc_min + soc_frac * (self._bat_V_oc_max - self._bat_V_oc_min)
        self.Bat_SOC    = self._bat_soc_pct
        self.Bat_V_volt = V_oc
        self.Bat_I_amp  = 0.0
        self.Bat_P_mw   = 0.0
        self.PV_P_W     = 0.0
        self.PV_V_volt  = 0.0
        self.PV_I_amp   = 0.0
        self.PV_P_mw    = 0.0
        self.Total_P_loss_mw   = 0.0
        self.Total_Q_loss_mvar = 0.0
        # Per-line and per-branch losses/currents (zeros at init)
        for lk in self._LINE_KEYS:
            setattr(self, f'P_loss_{lk}_mw',   0.0)
            setattr(self, f'Q_loss_{lk}_mvar', 0.0)
            setattr(self, f'I_from_{lk}_kA',   0.0)
            setattr(self, f'I_to_{lk}_kA',     0.0)
        for bk in self._BRANCH_KEYS:
            setattr(self, f'P_loss_{bk}_mw',   0.0)
            setattr(self, f'Q_loss_{bk}_mvar', 0.0)
