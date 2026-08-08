#set math.equation(numbering: "(1)")

= Battery — Interface (opaque Simulink FMU)
No exposed mathematical model — internal SOC/voltage dynamics are compiled
inside `Battery_Simulink_fmi3.fmu`; only the I/O contract is known.

== Given

- $P_"charge"$ — charge power setpoint (W), applied by the mosaik wrapper
- $P_"load"$ — held fixed at $0$ by the wrapper (unused in this scenario)
- $"PF"$ — power factor input

== Outputs (as read by the wrapper)

$ P_"load,mw" = -P / 10^6, quad "SOC", quad V, quad I $

$P$, SOC, $V$, $I$ are computed internally by the FMU each 60 s step; the
wrapper only negates and unit-converts $P$ before exposing it to mosaik —
the sign flip is a load/generation convention choice, not a physical law.
