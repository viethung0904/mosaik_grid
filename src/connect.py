"""
test_connection.py — generate mosaik world.connect() calls from the Neo4j graph topology.

Reads three edge types from the graph and prints the corresponding mosaik
connection statements to stdout:

  (:Substation)-[:LINE]->(:Substation)
      → from-bus  voltages to line (time_shifted)
      → to-bus    voltages to line (time_shifted)
      → line      currents to to-bus

  (:Substation)-[:CONNECT_TO]->(:Load)
      → load P/Q  to substation

  (:Substation)-[:CONNECT_TO {side:'HV'}]->(:Transformer)
  (:Substation)-[:CONNECT_TO {side:'LV'}]->(:Transformer)
      → v_source  voltages to transformer

Variable naming convention (matches scenario_test.py style):
  Substation  "N1"       → sub_n1
  Line        N1 → N2    → line_n1_n2
  Load        "Load_1A"  → load_1a
  Transformer "TR1"      → tr1
"""

import os
import sys
from dotenv import load_dotenv
from neo4j import GraphDatabase

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)


# ── helpers ──────────────────────────────────────────────────────────────────

def _var(name: str) -> str:
    """Normalise a graph node/rel name to a Python variable name fragment."""
    return name.lower().replace('-', '_').replace(' ', '_')


def _sub_var(node_name: str) -> str:
    return f'sub_{_var(node_name)}'


def _line_var(from_name: str, to_name: str) -> str:
    return f'line_{_var(from_name)}_{_var(to_name)}'


def _load_var(load_name: str) -> str:
    return _var(load_name)


def _tr_var(tr_name: str) -> str:
    return _var(tr_name)


# ── graph queries ─────────────────────────────────────────────────────────────

def fetch_edges():
    """Return (line_edges, load_edges, transformer_edges) from Neo4j."""
    load_dotenv(os.path.join(PROJECT_DIR, '.env'))
    uri  = os.getenv('NEO4J_URI')
    user = os.getenv('NEO4J_USERNAME')
    pwd  = os.getenv('NEO4J_PASSWORD')
    db   = os.getenv('NEO4J_DATABASE')

    driver = GraphDatabase.driver(uri, auth=(user, pwd))
    try:
        with driver.session(database=db) as session:

            # LINE edges between substations
            line_edges = session.run(
                'MATCH (a:Substation)-[l:LINE]->(b:Substation) '
                'RETURN a.name AS from_sub, b.name AS to_sub '
                'ORDER BY a.name, b.name'
            ).data()

            # CONNECT_TO edges from Substation to Load
            load_edges = session.run(
                'MATCH (s:Substation)-[:CONNECT_TO]->(l:Load) '
                'RETURN s.name AS sub_name, l.name AS load_name '
                'ORDER BY s.name, l.name'
            ).data()

            # Transformer edges:
            #   HV: (:Substation)-[:CONNECT_TO {side:'HV'}]->(:Transformer)
            #   LV: (:Transformer)-[:CONNECT_TO {side:'LV'}]->(:Substation)
            transformer_edges = session.run(
                'MATCH (hv:Substation)-[:CONNECT_TO {side:"HV"}]->(t:Transformer)'
                '-[:CONNECT_TO {side:"LV"}]->(lv:Substation) '
                'RETURN hv.name AS hv_sub, t.name AS tr_name, lv.name AS lv_sub '
                'ORDER BY t.name'
            ).data()

    finally:
        driver.close()

    return line_edges, load_edges, transformer_edges


# ── code generators ───────────────────────────────────────────────────────────

def gen_transformer_connections(transformer_edges: list) -> list[str]:
    """V_source → Transformer voltage connections."""
    lines = []
    for tr in transformer_edges:
        tr_v = _tr_var(tr['tr_name'])
        lines.append(f'# {tr["tr_name"]}: HV bus {tr["hv_sub"]} / LV bus {tr["lv_sub"]}')
        lines.append(f"world.connect(v_source, {tr_v}, ('V_source_mag',   'V1_mag'))")
        lines.append(f"world.connect(v_source, {tr_v}, ('V_source_angle', 'V1_angle'))")
        lines.append('')
    return lines


def gen_line_connections(line_edges: list, mv_voltage_var: str = 'V_MV_KV') -> list[str]:
    """
    For each LINE edge:
      - from-substation → line : from-bus voltages (time_shifted)
      - to-substation   → line : to-bus  voltages (time_shifted)
      - line            → to-substation : injected currents
    """
    lines = []
    for edge in line_edges:
        fs  = edge['from_sub']
        ts  = edge['to_sub']
        fv  = _sub_var(fs)
        tv  = _sub_var(ts)
        lv  = _line_var(fs, ts)

        lines.append(f'# Line {fs} → {ts}')
        lines.append(
            f"world.connect({fv}, {lv}, ('V_mag_kv', 'V_from_mag_kv'), "
            f"time_shifted=True, initial_data={{'V_mag_kv': {mv_voltage_var}}})"
        )
        lines.append(
            f"world.connect({fv}, {lv}, ('V_ang_deg', 'V_from_ang_deg'), "
            f"time_shifted=True, initial_data={{'V_ang_deg': 0.0}})"
        )
        lines.append(
            f"world.connect({tv}, {lv}, ('V_mag_kv', 'V_to_mag_kv'),   "
            f"time_shifted=True, initial_data={{'V_mag_kv': {mv_voltage_var}}})"
        )
        lines.append(
            f"world.connect({tv}, {lv}, ('V_ang_deg', 'V_to_ang_deg'), "
            f"time_shifted=True, initial_data={{'V_ang_deg': 0.0}})"
        )
        lines.append(f"world.connect({lv}, {tv}, ('I_to_re', 'I_in_re'))")
        lines.append(f"world.connect({lv}, {tv}, ('I_to_im', 'I_in_im'))")
        lines.append('')
    return lines


def gen_load_connections(load_edges: list) -> list[str]:
    """Load → Substation P/Q connections."""
    lines = []
    for edge in load_edges:
        lv = _load_var(edge['load_name'])
        sv = _sub_var(edge['sub_name'])
        lines.append(f'# {edge["load_name"]} → {edge["sub_name"]}')
        lines.append(f"world.connect({lv}, {sv}, ('P_load_mw',   'P_load_mw'))")
        lines.append(f"world.connect({lv}, {sv}, ('Q_load_mvar', 'Q_load_mvar'))")
        lines.append('')
    return lines


def gen_collector_connections(
    transformer_edges: list,
    line_edges: list,
) -> list[str]:
    """
    Collector monitoring connections:
      - Slack bus (LV side of each transformer) → V_mag_kv
      - Each substation on a LINE → V_mag_kv, V_ang_deg
      - Each LINE → P_loss_mw, Q_loss_mvar
      - v_source → V_source_mag (HV monitoring)
      - Each transformer → V2 (LV voltage monitoring)
    """
    lines = []

    # Slack buses = LV side of each transformer
    lines.append('# Slack bus voltage magnitude')
    for tr in transformer_edges:
        sv = _sub_var(tr['lv_sub'])
        key = f'V_slack_{sv}_mag_kv'
        lines.append(f"world.connect({sv}, collector, ('V_mag_kv', '{key}'))")
    lines.append('')

    # Load buses = all substations appearing in LINE edges (unique, ordered)
    seen: set[str] = set()
    bus_order: list[str] = []
    for edge in line_edges:
        for name in (edge['from_sub'], edge['to_sub']):
            if name not in seen:
                seen.add(name)
                bus_order.append(name)

    lines.append('# Load bus voltages (magnitude + angle)')
    for name in bus_order:
        sv  = _sub_var(name)
        mag_key = f'V_{sv}_mag_kv'
        ang_key = f'V_{sv}_ang_deg'
        lines.append(f"world.connect({sv}, collector, ('V_mag_kv', '{mag_key}'))")
        lines.append(f"world.connect({sv}, collector, ('V_ang_deg', '{ang_key}'))")
    lines.append('')

    # Line losses
    lines.append('# Line active / reactive power losses')
    for edge in line_edges:
        lv       = _line_var(edge['from_sub'], edge['to_sub'])
        p_key    = f'P_loss_{lv}_mw'
        q_key    = f'Q_loss_{lv}_mvar'
        lines.append(f"world.connect({lv}, collector, ('P_loss_mw',   '{p_key}'))")
        lines.append(f"world.connect({lv}, collector, ('Q_loss_mvar', '{q_key}'))")
    lines.append('')

    # HV source monitoring
    lines.append('# HV source monitoring')
    lines.append("world.connect(v_source, collector, ('V_source_mag', 'V_HV_mag'))")
    lines.append('')

    # Transformer LV voltage monitoring
    lines.append('# Transformer LV voltage monitoring')
    for tr in transformer_edges:
        tr_v   = _tr_var(tr['tr_name'])
        tr_key = f'V_{tr_v}_LV'
        lines.append(f"world.connect({tr_v}, collector, ('V2', '{tr_key}'))")
    lines.append('')

    return lines


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    print('Fetching edges from GraphDB...')
    line_edges, load_edges, transformer_edges = fetch_edges()
    print(
        f'  {len(transformer_edges)} transformer edge(s), '
        f'{len(line_edges)} line edge(s), '
        f'{len(load_edges)} load edge(s)\n'
    )

    sections = [
        ('# ── Transformer connections ────────────────────────────────────────────',
         gen_transformer_connections(transformer_edges)),
        ('# ── Line connections (LIM time-shifted) ────────────────────────────────',
         gen_line_connections(line_edges)),
        ('# ── Load connections ────────────────────────────────────────────────────',
         gen_load_connections(load_edges)),
        ('# ── Collector connections ───────────────────────────────────────────────',
         gen_collector_connections(transformer_edges, line_edges)),
    ]

    for header, code_lines in sections:
        print(header)
        print('\n'.join(code_lines))


if __name__ == '__main__':
    main()
