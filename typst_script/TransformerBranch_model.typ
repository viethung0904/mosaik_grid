#set math.equation(numbering: "(1)")

= Transformer Branch — Mathematical Model
Series impedance (HV-referred) + ideal transformer, LIM current-injection form

== Given

- $underline(V)_"HV" = V_"HV" angle.l theta_"HV"$, $underline(V)_"LV" = V_"LV" angle.l theta_"LV"$ — terminal voltages (kV)
- $underline(Z) = r_"hv" + j x_"hv"$ (Ω) — series impedance referred to HV side (`r_hv_ohm`, `x_hv_ohm`)
- $t = u_1 / u_2$ — turns ratio (`rated_u1_kv` / `rated_u2_kv`)

== Branch currents

$ underline(I)_s = underline(Y)_s (underline(V)_"HV" - t underline(V)_"LV"), quad underline(Y)_s = 1 / underline(Z) $

$ underline(I)_"in,HV" = -underline(I)_s, quad underline(I)_"in,LV" = t underline(I)_s $

$ mat(underline(I)_"in,HV"; underline(I)_"in,LV") =
  mat(-underline(Y)_s, t underline(Y)_s; t underline(Y)_s, -t^2 underline(Y)_s)
  mat(underline(V)_"HV"; underline(V)_"LV") $

== Series losses

$ P_"loss" = |underline(I)_s|^2 r_"hv" "(MW)", quad
  Q_"loss" = |underline(I)_s|^2 x_"hv" "(MVAr)" $

== Master coupling

$ underline(Y)_("self","HV") += underline(Y)_s, quad
  underline(Y)_("self","LV") += t^2 underline(Y)_s $

$ underline(I)_("net","HV") += underline(I)_"in,HV", quad
  underline(I)_("net","LV") += underline(I)_"in,LV" $

Nodal KCL accumulation before each `SubstationFMU.do_step()`.
