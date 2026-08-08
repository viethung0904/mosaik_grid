#set math.equation(numbering: "(1)")

= PV Array — Mathematical Model
Single-diode model + Perturb & Observe MPPT, array scaling

== Given

- $S, T$ — solar irradiance (W/m²) and cell temperature (K)
- $I_(p h , r), V_(o c , r), mu_I, mu_V$ — reference photocurrent, open-circuit voltage, and their temperature coefficients (at $S_r, T_r$)
- $N_s, R_s, R_"sh", n$ — cells in series per string, series/shunt resistance, diode ideality factor
- $N_"series", N_"parallel"$ — strings in series / parallel in the array

== Photocurrent and saturation current

$ I_(p h) = (S / S_r) (I_(p h , r) + mu_I (T - T_r)) $

$ I_0 = (I_(p h , r) + mu_I (T - T_r)) / (exp(q (V_(o c , r) + mu_V (T - T_r)) / (n k T)) - 1) $

== I-V characteristic (single string)

Implicit single-diode equation, solved for $I$ at a given operating voltage $V$ (Newton solve):

$ I - I_(p h) + I_0 (exp((q (V + I R_s)) / (n k T N_s)) - 1) + (V + I R_s) / R_"sh" = 0, quad P = V I $

== MPPT (Perturb & Observe, default)

$ Delta P = P_k - P_(k-1), quad Delta V = V_k - V_(k-1) $

$ V_(k+1) = V_k + op("sgn") dot delta_V, quad
  op("sgn") = cases(
    +1 & (Delta P > 0 "and" Delta V > 0) "or" (Delta P <= 0 "and" Delta V <= 0),
    -1 & "otherwise"
  ) $

($delta_V$ = MPPT step size; result clipped to $[V_min, V_max]$.)

== Array scaling

$ V_"arr" = N_"series" V, quad I_"arr" = N_"parallel" I, quad P_"arr" = V_"arr" I_"arr" $

At night ($|P_"arr"| < 1$ W), $V_"arr" -> 0$.
