#set math.equation(numbering: "(1)")

= Substation Bus — Mathematical Model
Newton-Raphson phasor-domain power flow, local KCL solve

== Given

- $underline(I)_"in"$ — net current injected by adjacent line/transformer FMUs (kA), time-shifted
- $P, Q$ — load demand at the bus (MW, MVAr)
- $underline(Y)_"self" = Y_"re" + j Y_"im"$ — bus self-admittance (Σ of adjacent series/transformer terms)
- $B$ — total shunt susceptance at the bus (Σ $b_"ch" / 2$)
- $omega$ — outer Gauss-Jacobi relaxation factor

== Bus equation

Strip the stale self-term baked into the time-shifted $underline(I)_"in"$, leaving neighbor contributions only:

$ underline(I)_"nbr" = underline(I)_"in" + underline(Y)_"self" underline(V)_"old" $

KCL residual solved for the unknown bus voltage $underline(V)$ ($S = P + j Q$):

$ f(underline(V)) = underline(I)_"nbr" - op("conj")(S \/ underline(V)) - (underline(Y)_"self" + j B) underline(V) = 0 $

== PQ bus (load bus)

Solved via rectangular Newton-Raphson (tolerance $10^(-10)$ kA) for $underline(V)_"NR"$.

== PV bus (synchronous machine)

Voltage magnitude pinned at $V_"reg"$; angle solved analytically from the real-power balance:

$ K = (P + V_"reg"^2 Y_"re") / V_"reg", quad
  theta = angle.l underline(I)_"nbr" plus.minus arccos(K / abs(underline(I)_"nbr")) $

(root closest to the warm-start angle), giving $underline(V)_"NR" = V_"reg" angle.l theta$. $Q$ is unconstrained.

== Slack bus

$ underline(V) = V_"slack" angle.l theta_"slack" quad "(fixed)" $

== Outer relaxation

$ underline(V)_"out" = underline(V)_"old" + omega (underline(V)_"NR" - underline(V)_"old") $

Applied once per LIM sweep before $underline(V)$ is exposed to adjacent line/transformer FMUs.
