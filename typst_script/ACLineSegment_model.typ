#set math.equation(numbering: "(1)")

= AC Line Segment — Mathematical Model
π-equivalent, LIM current-injection form

This document derives the branch equations implemented by the
`ACLineSegment` FMU, which models a transmission line as a nominal
π-equivalent circuit and computes the current flowing between its two
terminals given both end voltages. It is intended to be coupled with
`SubstationFMU` bus instances through a LIM (Linear Iteration Method)
master, which accumulates signed current injections at each bus before
resolving the local bus voltage.

== Given

Terminal voltage phasors, referenced to a common ground, with kV / kA / Ω
kept mutually consistent throughout:

- $underline(V)_f = V_f angle.l theta_f$ — sending-end (from-bus) voltage (kV)
- $underline(V)_t = V_t angle.l theta_t$ — receiving-end (to-bus) voltage (kV)
- $underline(Z) = R + j X$ (Ω) — series impedance, from parameters `r_ohm`, `x_ohm`
- $b_"ch"$ (S) — total shunt (charging) susceptance of the line, split
  symmetrically between the two ends: $underline(Y)_h = j b_"ch" / 2$

== Series current

The series branch carries the current driven by the voltage difference
across the line impedance, exactly as in a simple two-terminal resistor:

$ underline(I)_"series" = (underline(V)_f - underline(V)_t) / underline(Z) $

If $|underline(Z)|$ is numerically degenerate (a modeled short), the
implementation falls back to $underline(I)_"series" = 0$ rather than
dividing by zero.

== Shunt currents

Half of the line's total charging susceptance is lumped at each
terminal in the standard nominal-π model, injecting a current that
leads its local terminal voltage by 90°:

$ underline(I)_("sh",f) = underline(Y)_h underline(V)_f, quad
  underline(I)_("sh",t) = underline(Y)_h underline(V)_t $

== Terminal currents

By Kirchhoff's current law at each terminal node, the current entering
the line from the from-bus equals the series current plus what is
diverted into the local shunt branch; the current leaving the line
into the to-bus is the series current less what the to-side shunt
branch has already drawn off:

$ underline(I)_"from" = underline(I)_"series" + underline(I)_("sh",f), quad
  underline(I)_"to" = underline(I)_"series" - underline(I)_("sh",t) $

$underline(I)_"from"$ is defined as the current leaving the from-bus
into the line; $underline(I)_"to"$ is defined as the current arriving
at the to-bus from the line — hence the shunt term is added on one
side and subtracted on the other. Equivalently, in nodal admittance
(two-port) form:

$ mat(underline(I)_"from"; underline(I)_"to") =
  mat(1/underline(Z) + j b_"ch"/2, -1/underline(Z); -1/underline(Z), 1/underline(Z) - j b_"ch"/2)
  mat(underline(V)_f; underline(V)_t) $

The diagonal terms are asymmetric ($+j b_"ch"/2$ at from, $-j b_"ch"/2$
at to) purely as a consequence of the from/to sign convention chosen
above, not a physical asymmetry of the line.

== Series losses

Only the series impedance dissipates real and reactive power; the
shunt branches are purely reactive (no conductance term is modeled),
so they contribute no resistive loss:

$ P_"loss" = |underline(I)_"series"|^2 R quad "(MW)", quad
  Q_"loss" = |underline(I)_"series"|^2 X quad "(MVAr)" $

== Master coupling

The LIM master accumulates signed nodal current injections from every
line and transformer branch connected to a bus, before advancing that
bus's `SubstationFMU`:

$ underline(I)_("net",f) -= underline(I)_"from", quad
  underline(I)_("net",t) += underline(I)_"to" $

This is the discretized nodal KCL boundary condition each substation
bus solves against: its own load/generation injection minus/plus the
sum of all connected line flows.