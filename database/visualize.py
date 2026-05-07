"""
Visualize the CIGRE MV graph from Neo4j Aura as an interactive HTML page.

Graph shape in Neo4j:
  (Ni:Substation)-[:LINE {ACLineSegment properties}]->(Nj:Substation)
  (Nk:Substation)-[:CONNECT_TO {terminal_id, side}]->(TRx:Transformer)-[:CONNECT_TO]->(Nl:Substation)
  (Nm:Substation)-[:CONNECT_TO {terminal_id}]->(Lo:Load)

Output: graph.html  (opens automatically in the default browser)
"""

import os
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

# ── visual style ──────────────────────────────────────────────────────────────
SUBSTATION_COLOR = "#4C9BE8"   # blue
TRANSFORMER_COLOR = "#2ECC71"  # green
LOAD_COLOR = "#E8A838"         # amber
SUBSTATION_SIZE = 28
TRANSFORMER_SIZE = 22
LOAD_SIZE = 16
SUBSTATION_SHAPE = "dot"
TRANSFORMER_SHAPE = "square"
LOAD_SHAPE = "triangle"


def fetch_graph():
    """Return (nodes, edges) from Neo4j.

    nodes : list of dicts  {id, label, node_type, **props}
    edges : list of dicts  {from_id, to_id, **rel_props}
    """
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USERNAME, NEO4J_PASSWORD))
    nodes, edges = [], []

    with driver.session(database=NEO4J_DATABASE) as session:
        # Fetch all nodes (Substation + Transformer + Load)
        result = session.run(
            """
            MATCH (n)
            WHERE n:Substation OR n:Transformer OR n:Load
            RETURN elementId(n) AS id,
                   labels(n)[0]  AS node_type,
                   properties(n) AS props
            """
        )
        for record in result:
            node = {
                "id": record["id"],
                "node_type": record["node_type"],
                **record["props"],
            }
            node["label"] = node.get("name", node["id"])
            nodes.append(node)

        # Fetch all relationships with properties
        result = session.run(
            """
            MATCH (a)-[r]->(b)
            WHERE (a:Substation OR a:Transformer) AND (b:Substation OR b:Transformer OR b:Load)
            RETURN elementId(a) AS from_id, elementId(b) AS to_id,
                   type(r) AS rel_type, properties(r) AS props
            """
        )
        for record in result:
            edges.append({
                "from_id": record["from_id"],
                "to_id": record["to_id"],
                "_rel_type": record["rel_type"],
                **record["props"],
            })

    driver.close()
    return nodes, edges


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

    # Add nodes (Substations + Transformers + Loads)
    for node in nodes:
        nt = node["node_type"]
        if nt == "Transformer":
            color, size, shape = TRANSFORMER_COLOR, TRANSFORMER_SIZE, TRANSFORMER_SHAPE
        elif nt == "Load":
            color, size, shape = LOAD_COLOR, LOAD_SIZE, LOAD_SHAPE
        else:
            color, size, shape = SUBSTATION_COLOR, SUBSTATION_SIZE, SUBSTATION_SHAPE
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
        # Dashed grey edges for Load connections
        is_load_edge = rel_type == "CONNECT_TO" and not edge.get("side")
        edge_color = "#888888" if is_load_edge else "#F4A261"
        net.add_edge(
            edge["from_id"],
            edge["to_id"],
            label=label,
            title=tooltip,
            font={"size": 11, "color": edge_color, "strokeWidth": 0},
            color={"color": edge_color, "highlight": "#ffffff"},
            dashes=is_load_edge,
        )

    return net


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
  <span style="color:#aaa;font-size:11px;">Hover nodes/edges for properties</span>
</div>
"""
    content = Path(html_path).read_text(encoding="utf-8")
    # Inject tooltip CSS and legend just before </body>
    content = content.replace("</body>", tooltip_css + legend + "\n</body>")
    Path(html_path).write_text(content, encoding="utf-8")


if __name__ == "__main__":
    print("Fetching graph from Neo4j …")
    nodes, edges = fetch_graph()
    print(f"  {sum(1 for n in nodes if n['node_type'] == 'Substation')} Substation nodes")
    print(f"  {sum(1 for n in nodes if n['node_type'] == 'Transformer')} Transformer nodes")
    print(f"  {sum(1 for n in nodes if n['node_type'] == 'Load')} Load nodes")
    print(f"  {len(edges)} edges")

    print("Building visualisation …")
    net = build_html_graph(nodes, edges)
    net.save_graph(OUTPUT_FILE)
    inject_legend(OUTPUT_FILE)

    abs_path = str(Path(OUTPUT_FILE).resolve())
    print(f"  Saved → {abs_path}")
    try:
        webbrowser.open(f"file://{abs_path}")
    except Exception:
        pass
    print(f"Done. Open the file manually: file://{abs_path}")
