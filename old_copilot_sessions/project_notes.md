# CIGRE MV Graph + Co-simulation Project

## Environment
- Workspace: `/home/hungpv/graph_database_test`
- Python: `/home/hungpv/miniforge3/envs/graph_database/bin/python`
- Neo4j Aura: URI=`neo4j+s://be1150b7.databases.neo4j.io`, user/db=`be1150b7`, pass=`hs68sYbB2EbNCkeFJAr4WK62QV0ZwWcBOglFVaGy87I`
- Libraries: `neo4j`, `python-dotenv`, `pyvis`, `mosaik`, `mosaik-api-v3`, `pythonfmu`

## Source Files (CGMES v2.4.15)
- `CIGREMV_reference_cgmes_v2_4_15_Equipment.xml`
- `CIGREMV_reference_cgmes_v2_4_15_Topology.xml`
- `CIGREMV_reference_cgmes_v2_4_15_StateVariables.xml`

## Graph Schema (Neo4j)
- `(:Substation)` — 15 nodes (N0–N14), properties: name, nominal_voltage_kv, region, rdf_id
- `(:Transformer)` — 2 nodes (TR1, TR2), hv_*/lv_* prefixed parameters
- `(:Load)` — 18 nodes, with p_mw/q_mvar from SvPowerFlow
- `(:Substation)-[:LINE {r,x,bch,…}]->(:Substation)` — 12 relationships
- `(:Substation)-[:CONNECT_TO {side:'HV'}]->(:Transformer)-[:CONNECT_TO {side:'LV'}]->(:Substation)` — 4 rels
- `(:Substation)-[:CONNECT_TO]->(:Load)` — 18 rels

## Network Topology
- Two feeders: Feeder 1 (slack=N1, 20 kV, buses N1–N11), Feeder 2 (slack=N12, 20 kV, buses N12–N14)
- N0 isolated (110 kV HV side of TR1)
- N8 is a junction: branches to N3 and N7; parent chain N1→N2→N8→N9→N10→N11

## Project Files
| File | Location | Status |
|------|----------|--------|
| `add_data.py` | `graph_database_test/` | Complete — parse CGMES XML → Neo4j |
| `visualize.py` | `graph_database_test/` | Complete — pyvis `graph.html` with loads |
| `inspect_graph.py` | `graph_database_test/` | Complete — CLI node/edge inspector |
| `grid_sim.py` | `graph_database_test/` | Complete — Mosaik GridSim (BFS + LIM) |
| `python_bus.py` | `graph_database_test/` | FMU skeleton for BusN14 |
| `substation_fmu.py` | `Python_Substation/` | Moved from graph_database_test/ |

## grid_sim.py Key Details
- Constants: `SLACK_BUS="N1"`, `SLACK_VOLTAGE=20.0`, `MAX_ITER=20`, `TOLERANCE=1e-6`, `OMEGA=314.16`
- `load_network_from_neo4j()` → buses, lines, nominal, slack_buses
- `_connected_components()` → detects 2 feeders
- `backward_forward_sweep()` — validated, ~0.1 kV error vs StateVariables, ~0.3 ms
- `lim_sweep(..., omega=0.5, max_iter=5000)` — phasor Jacobi, converges ~3500 iters, ~90 ms
- `solve_all_feeders()` / `solve_all_feeders_lim()` — multi-feeder wrappers
- `lim_stability_dt()` — physical EMTP dt per branch (range 6.5–103.5 µs)
- `GridSim(mosaik_api_v3.Simulator)` — Mosaik class with `method='bfs'|'lim'` parameter

## LIM Key Findings
- Radial/tree networks: Jacobi spectral radius ρ ≥ 1 at ALL buses (Y_off ≈ Y_self, shunt negligible)
- Under-relaxation `omega=0.5` required; reduces effective ρ ≈ 0.5 → ~3500 iterations to converge
- LIM satisfies phasor KCL better than BFS (smaller residuals) but is ~300× slower
- Real LIM for co-simulation = one-step FMI/Mosaik coupling delay (built into FMI protocol)
- Physical EMTP dt: tightest = N11→N10 = 6.5 µs; Mosaik QSTS steps (1–60 s) are safely stable

## LIM Stability dt per Branch
| Branch | dt_max |
|--------|--------|
| N1→N2  | 54.3 µs |
| N2→N3  | 103.5 µs |
| N3→N4  | 17.5 µs |
| N4→N5  | 17.3 µs |
| N5→N6  | 32.3 µs |
| N8→N3  | 42.9 µs |
| N8→N7  | 35.0 µs |
| N9→N8  | 11.6 µs |
| N10→N9 | 18.3 µs |
| N11→N10| 6.5 µs ← tightest |
| N12→N13| 94.2 µs |
| N13→N14| 57.6 µs |

Physical dt used = 0.9 × min = **5.8 µs** (10% safety margin)

## BusN14 FMU (python_bus.py)

### Variables
| Name | Type | Description |
|------|------|-------------|
| `r`, `x`, `bch` | param | Line impedance (Ω) and shunt susceptance (S) |
| `P_load`, `Q_load` | param | Fixed passive load (MW, MVAr) |
| `V_up_mag`, `V_up_ang` | input | Parent bus voltage from previous FMI step |
| `P_inject`, `Q_inject` | input | Real-time DER dispatch (battery/PV/EV) — positive = generation |
| `V_bus_mag`, `V_bus_ang` | output | This bus voltage |
| `P_flow`, `Q_flow` | output | Complex power entering the line at sending end |

### do_step Logic
```
S_load = (P_load - P_inject) + j(Q_load - Q_inject)   # net demand
I_load = conj(S_load / conj(V_up))                     # backward pass
V14    = V_up - Z * I_load                             # forward pass (Ohm's law)
S_line = V_up * conj(I_load)                           # sending-end power
```

### Known Approximations / TODOs
- `I_load` uses `V_up` instead of `V_bus` (correct B/F uses the bus's own voltage) → ~0.07 kV error at N14
- `bch` shunt is defined as a parameter but not used in `do_step` yet
- For an inner bus, `I_children` from downstream FMUs must be summed in the backward pass
- `S_line` line can be simplified to `V_up * I_load.conjugate()` (equivalent, cheaper)

### FMI as LIM
The one-communication-step delay between FMUs IS the LIM latency — no extra numerical trick needed.
Decompose grid into one FMU per bus + one FMU per branch, wire via FMI master.
Safe Mosaik step size for a droop controller with gain k_d:
  Δt_mosaik < 2 × |Z_Thevenin(bus)| / k_d
