"""
Visualize the CIGRE MV graph from Neo4j Aura as an interactive HTML page.

Graph shape in Neo4j:
  (Ni:Substation)-[:LINE {ACLineSegment properties}]->(Nj:Substation)
  (Nk:Substation)-[:CONNECT_TO {terminal_id, side}]->(TRx:Transformer)-[:CONNECT_TO]->(Nl:Substation)
  (Nm:Substation)-[:CONNECT_TO {terminal_id}]->(Lo:Load)

Output: graph.html  (opens automatically in the default browser)
"""

import os
import json
import webbrowser
from pathlib import Path

from dotenv import load_dotenv
from neo4j import GraphDatabase
from pyvis.network import Network

load_dotenv()

NEO4J_URI = os.getenv("NEO4J_URI")
NEO4J_USERNAME = os.getenv("NEO4J_USERNAME")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD")
NEO4J_DATABASE = os.getenv("NEO4J_DATABASE")

OUTPUT_FILE = "graph.html"
TOPOLOGY_EXPORT_FILE = "topology_export.json"

# ── visual style ──────────────────────────────────────────────────────────────
SUBSTATION_COLOR = "#4C9BE8"   # blue
TRANSFORMER_COLOR = "#2ECC71"  # green
LOAD_COLOR = "#E8A838"         # amber
BATTERY_COLOR = "#27AE60"      # dark green
PV_COLOR = "#F39C12"           # orange-yellow
SUBSTATION_SIZE = 28
TRANSFORMER_SIZE = 22
LOAD_SIZE = 16
BATTERY_SIZE = 20
PV_SIZE = 20
SUBSTATION_SHAPE = "dot"
TRANSFORMER_SHAPE = "square"
LOAD_SHAPE = "triangle"
BATTERY_SHAPE = "database"
PV_SHAPE = "star"


def fetch_graph():
    """Return (nodes, edges) from Neo4j.

    nodes : list of dicts  {id, label, node_type, **props}
    edges : list of dicts  {from_id, to_id, **rel_props}
    """
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USERNAME, NEO4J_PASSWORD))
    nodes, edges = [], []

    scheme_name = "Grid Simulation"
    with driver.session(database=NEO4J_DATABASE) as session:
        # Fetch all relationships in the loaded scheme, regardless of type.
        # Excludes GridMetadata bookkeeping nodes so any CIM scheme works.
        result = session.run(
            """
            MATCH (a)-[r]->(b)
            WHERE NOT 'GridMetadata' IN labels(a)
              AND NOT 'GridMetadata' IN labels(b)
            RETURN elementId(a) AS from_id,
                   elementId(b) AS to_id,
                   type(r) AS rel_type,
                   properties(r) AS props
            """
        )
        node_ids = set()
        for record in result:
            from_id = record["from_id"]
            to_id = record["to_id"]
            node_ids.add(from_id)
            node_ids.add(to_id)
            edges.append({
                "from_id": from_id,
                "to_id": to_id,
                "_rel_type": record["rel_type"],
                **record["props"],
            })

        # Read scheme name from GridMetadata node (written by add_data_slack_detection.py)
        meta = session.run(
            "MATCH (m:GridMetadata) RETURN m.scheme_name AS name ORDER BY m.loaded_at DESC LIMIT 1"
        ).single()
        if meta and meta["name"]:
            scheme_name = meta["name"]

        if not node_ids:
            driver.close()
            return [], [], scheme_name

        # Fetch only nodes participating in the detected topology.
        result = session.run(
            """
            UNWIND $node_ids AS nid
            MATCH (n)
            WHERE elementId(n) = nid
            RETURN elementId(n) AS id,
                   labels(n)[0]  AS node_type,
                   properties(n) AS props
            """,
            node_ids=list(node_ids),
        )
        for record in result:
            node = {
                "id": record["id"],
                "node_type": record["node_type"],
                **record["props"],
            }
            node["label"] = node.get("name", node["id"])
            nodes.append(node)

    driver.close()
    return nodes, edges, scheme_name


def fmt_props_html(label: str, props: dict, skip: set) -> str:
    """Format properties as newline-separated key : value lines."""
    items = sorted((k, v) for k, v in props.items() if k not in skip and v is not None)
    lines = [f"[{label}]"] + [f"{k} : {v}" for k, v in items]
    return "\n".join(lines)


def build_tooltip(node: dict) -> str:
    """Build an HTML tooltip with all node properties."""
    skip = {"id", "node_type", "label", "rdf_id"}
    return fmt_props_html(node["node_type"], node, skip)


def build_html_graph(nodes, edges) -> Network:
    net = Network(
        height="92vh",
        width="100%",
        bgcolor="#1a1a2e",
        font_color="white",
        directed=True,
        notebook=False,
        cdn_resources="in_line",
    )

    net.set_options("""
    {
      "physics": {
        "solver": "forceAtlas2Based",
        "forceAtlas2Based": {
          "gravitationalConstant": -80,
          "centralGravity": 0.01,
          "springLength": 120,
          "springConstant": 0.08
        },
        "stabilization": { "iterations": 200 }
      },
      "edges": {
        "arrows": { "to": { "enabled": true, "scaleFactor": 0.7 } },
        "color": { "color": "#aaaaaa", "highlight": "#ffffff" },
        "width": 1.5,
        "smooth": { "type": "curvedCW", "roundness": 0.1 }
      },
      "interaction": {
        "hover": true,
        "tooltipDelay": 100,
        "navigationButtons": true,
        "keyboard": true
      }
    }
    """)

    # Add nodes for all detected element types in the active topology.
    style_by_type = {
        "Substation": (SUBSTATION_COLOR, SUBSTATION_SIZE, SUBSTATION_SHAPE),
        "Transformer": (TRANSFORMER_COLOR, TRANSFORMER_SIZE, TRANSFORMER_SHAPE),
        "Load": (LOAD_COLOR, LOAD_SIZE, LOAD_SHAPE),
        "Battery": (BATTERY_COLOR, BATTERY_SIZE, BATTERY_SHAPE),
        "PV": (PV_COLOR, PV_SIZE, PV_SHAPE),
    }
    default_style = ("#95A5A6", 18, "dot")

    for node in nodes:
        nt = node["node_type"]
        color, size, shape = style_by_type.get(nt, default_style)
        net.add_node(
            node["id"],
            label=node["label"],
            title=build_tooltip(node),
            color=color,
            size=size,
            shape=shape,
            borderWidth=2,
            font={"size": 13, "bold": True},
        )

    # Add edges: label shows line name (if present), tooltip shows all properties
    for edge in edges:
        skip = {"from_id", "to_id", "_rel_type"}
        rel_type = edge.get("_rel_type", "CONNECT_TO")
        label = edge.get("name") or edge.get("side", "")
        tooltip = fmt_props_html(rel_type, edge, skip)
        # Dashed grey edges for Load connections; solid green for Battery connections
        is_load_edge = rel_type == "CONNECT_TO" and not edge.get("side")
        pv_ids = {n["id"] for n in nodes if n.get("node_type") == "PV"}
        battery_ids = {n["id"] for n in nodes if n.get("node_type") == "Battery"}
        is_battery_edge = rel_type == "CONNECT_TO" and edge.get("to_id") in battery_ids
        is_pv_edge = rel_type == "CONNECT_TO" and edge.get("to_id") in pv_ids
        if is_battery_edge:
            edge_color = "#27AE60"
        elif is_pv_edge:
            edge_color = "#F39C12"
        elif is_load_edge:
            edge_color = "#888888"
        else:
            edge_color = "#F4A261"
        net.add_edge(
            edge["from_id"],
            edge["to_id"],
            label=label,
            title=tooltip,
            font={"size": 11, "color": edge_color, "strokeWidth": 0},
            color={"color": edge_color, "highlight": "#ffffff"},
            dashes=is_load_edge and not is_battery_edge and not is_pv_edge,
        )

    return net


def export_topology_json(nodes, edges, output_path: str, scheme_name: str = ""):
    """Export detected scheme elements/topology for downstream use."""
    node_type_counts = {}
    for n in nodes:
        nt = n.get("node_type", "Unknown")
        node_type_counts[nt] = node_type_counts.get(nt, 0) + 1

    rel_type_counts = {}
    for e in edges:
        rt = e.get("_rel_type", "UNKNOWN")
        rel_type_counts[rt] = rel_type_counts.get(rt, 0) + 1

    payload = {
        "scheme_name": scheme_name,
        "summary": {
            "node_count": len(nodes),
            "edge_count": len(edges),
            "node_type_counts": dict(sorted(node_type_counts.items())),
            "relationship_type_counts": dict(sorted(rel_type_counts.items())),
        },
        "nodes": [
            {
                "id": n.get("id"),
                "label": n.get("label"),
                "type": n.get("node_type"),
                "properties": {
                    k: v
                    for k, v in n.items()
                    if k not in {"id", "label", "node_type"}
                },
            }
            for n in nodes
        ],
        "edges": [
            {
                "from": e.get("from_id"),
                "to": e.get("to_id"),
                "type": e.get("_rel_type"),
                "properties": {
                    k: v
                    for k, v in e.items()
                    if k not in {"from_id", "to_id", "_rel_type"}
                },
            }
            for e in edges
        ],
    }

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)


def inject_legend(html_path: str):
    """Inject legend panel and tooltip CSS into the pyvis HTML output."""
    tooltip_css = """
<style>
.vis-tooltip {
  white-space: pre-line !important;
  font-family: monospace !important;
  font-size: 12px !important;
  background: #1e1e2e !important;
  color: #e0e0e0 !important;
  border: 1px solid #555 !important;
  border-radius: 6px !important;
  padding: 8px 12px !important;
  max-width: 400px !important;
}
</style>
"""
    legend = """
<div style="
  position:fixed; top:14px; left:14px; z-index:9999;
  background:rgba(26,26,46,0.88); border:1px solid #444;
  border-radius:8px; padding:12px 16px; color:#fff;
  font-family:sans-serif; font-size:13px; line-height:1.8;
">
  <b style="font-size:14px;">CIGRE MV Network</b><br>
  <span style="display:inline-block;width:14px;height:14px;border-radius:50%;
        background:#4C9BE8;vertical-align:middle;margin-right:6px;"></span>Substation<br>
  <span style="display:inline-block;width:14px;height:14px;
        background:#2ECC71;vertical-align:middle;margin-right:6px;"></span>Transformer<br>
  <span style="display:inline-block;width:0;height:0;
        border-left:7px solid transparent;border-right:7px solid transparent;
        border-bottom:14px solid #E8A838;vertical-align:middle;margin-right:6px;"></span>Load<br>
  <span style="display:inline-block;width:40px;height:3px;
        background:#F4A261;vertical-align:middle;margin-right:6px;"></span>Line / Transformer connection<br>
  <span style="display:inline-block;width:40px;height:0;border-top:2px dashed #888;vertical-align:middle;margin-right:6px;"></span>Load connection<br>
  <span style="display:inline-block;width:14px;height:14px;border-radius:3px;
        background:#27AE60;vertical-align:middle;margin-right:6px;border:2px solid #1E8449;"></span>Battery<br>
  <span style="display:inline-block;width:14px;height:14px;
        clip-path:polygon(50% 0%,61% 35%,98% 35%,68% 57%,79% 91%,50% 70%,21% 91%,32% 57%,2% 35%,39% 35%);
        background:#F39C12;vertical-align:middle;margin-right:6px;"></span>PV Array<br>
  <span style="color:#aaa;font-size:11px;">Hover nodes/edges for properties</span>
</div>
"""
    content = Path(html_path).read_text(encoding="utf-8")
    # Inject tooltip CSS and legend just before </body>
    content = content.replace("</body>", tooltip_css + legend + "\n</body>")
    Path(html_path).write_text(content, encoding="utf-8")


if __name__ == "__main__":
    print("Fetching graph from Neo4j …")
    nodes, edges, scheme_name = fetch_graph()
    print(f"  Scheme: {scheme_name!r}")
    type_counts = {}
    for n in nodes:
        nt = n.get("node_type", "Unknown")
        type_counts[nt] = type_counts.get(nt, 0) + 1
    for nt in sorted(type_counts):
        print(f"  {type_counts[nt]} {nt} node(s)")
    print(f"  {len(edges)} edges")

    print("Building visualisation …")
    net = build_html_graph(nodes, edges)
    net.save_graph(OUTPUT_FILE)
    inject_legend(OUTPUT_FILE)
    export_topology_json(nodes, edges, TOPOLOGY_EXPORT_FILE, scheme_name=scheme_name)

    abs_path = str(Path(OUTPUT_FILE).resolve())
    abs_topology_path = str(Path(TOPOLOGY_EXPORT_FILE).resolve())
    print(f"  Saved → {abs_path}")
    print(f"  Topology export → {abs_topology_path}")
    try:
        webbrowser.open(f"file://{abs_path}")
    except Exception:
        pass
    print(f"Done. Open the file manually: file://{abs_path}")
