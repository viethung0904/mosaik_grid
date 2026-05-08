"""
Live dashboard helper: HTTP server + live_dashboard.html generator.

Starts a background SimpleHTTPRequestHandler so the browser can poll
output.json and sim_status.json while the mosaik simulation is running.
"""
import re
import subprocess
import sys
import time as _time
import os
import json


# ── HTTP server ───────────────────────────────────────────────────────────────

def start_live_server(directory: str, port: int = 8765):
    """
    Start an HTTP server as a *separate process* serving *directory* on *port*.
    Using a subprocess (not a thread) ensures the server keeps running even
    after the simulation script exits or is killed.
    Returns (proc, url) or (None, None) on failure.
    """
    # Kill any stale server already on this port
    try:
        import socket
        with socket.socket() as s:
            s.settimeout(0.3)
            if s.connect_ex(('localhost', port)) == 0:
                # Something is already listening — try to kill it
                subprocess.run(['fuser', '-k', f'{port}/tcp'],
                               capture_output=True, check=False)
                _time.sleep(0.3)
    except Exception:
        pass

    try:
        proc = subprocess.Popen(
            [sys.executable, '-m', 'http.server', str(port),
             '--directory', directory],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _time.sleep(0.4)          # give the server a moment to bind
        if proc.poll() is not None:
            raise RuntimeError(f'server exited immediately (code {proc.returncode})')
        url = f'http://localhost:{port}/live_dashboard.html'
        return proc, url
    except Exception as exc:
        print(f'[live_server] Warning: could not start HTTP server on port {port}: {exc}')
        return None, None


# ── Dashboard generator ───────────────────────────────────────────────────────

def generate_live_dashboard(graph_html_path: str, output_path: str) -> bool:
    """
    Read graph.html, extract the vis.js nodes/edges/options, and write
    live_dashboard.html with a polling-based live chart panel.
    Returns True on success.
    """
    if not os.path.exists(graph_html_path):
        print(f'[live_server] Warning: {graph_html_path} not found; skipping dashboard generation.')
        return False

    with open(graph_html_path, 'r') as f:
        content = f.read()

    m_nodes = re.search(r'nodes = new vis\.DataSet\((\[.*?\])\);', content, re.DOTALL)
    m_edges = re.search(r'edges = new vis\.DataSet\((\[.*?\])\);', content, re.DOTALL)
    m_opts  = re.search(r'var options = (\{.*?\});',               content, re.DOTALL)

    if not (m_nodes and m_edges and m_opts):
        print('[live_server] Warning: could not parse graph.html; skipping dashboard generation.')
        return False

    html = _TEMPLATE
    html = html.replace('NODES_PLACEHOLDER',   m_nodes.group(1))
    html = html.replace('EDGES_PLACEHOLDER',   m_edges.group(1))
    html = html.replace('OPTIONS_PLACEHOLDER', m_opts.group(1))

    with open(output_path, 'w') as f:
        f.write(html)

    return True


# ── HTML template ─────────────────────────────────────────────────────────────
# Uses polling (fetch + setInterval) instead of embedded static data.

_TEMPLATE = r"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Grid Simulation — Live Dashboard</title>
<script src="https://unpkg.com/vis-network/standalone/umd/vis-network.min.js"></script>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: "Segoe UI", sans-serif;
  background: #0d0d1a;
  color: #e0e0e0;
  height: 100vh;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

/* ── Header ── */
#header {
  background: #16213e;
  border-bottom: 1px solid #2d4a7a;
  padding: 6px 16px;
  display: flex;
  align-items: center;
  gap: 12px;
  flex-shrink: 0;
}
#header h1 { font-size: 14px; color: #7eb8f7; white-space: nowrap; }

#status-badge {
  display: flex; align-items: center; gap: 6px;
  padding: 3px 10px;
  border-radius: 12px;
  font-size: 11px; font-weight: bold;
  background: #1a2a1a; color: #2ecc71;
  border: 1px solid #2ecc71;
  white-space: nowrap;
}
#status-badge.done { background: #1a1a2a; color: #888; border-color: #444; }
#status-dot {
  width: 8px; height: 8px; border-radius: 50%;
  background: #2ecc71;
  animation: pulse 1.2s ease-in-out infinite;
}
#status-badge.done #status-dot { background: #555; animation: none; }
@keyframes pulse {
  0%,100% { opacity: 1; } 50% { opacity: 0.3; }
}

#progress-wrap {
  flex: 1; min-width: 80px; max-width: 220px;
  background: #1a1a30;
  border-radius: 6px; overflow: hidden; height: 16px;
  border: 1px solid #2d4a7a;
}
#progress-bar {
  height: 100%; width: 0%;
  background: linear-gradient(90deg, #2d4a7a, #4C9BE8);
  transition: width 0.4s;
}
#progress-label { font-size: 10px; color: #888; white-space: nowrap; }

/* ── Main layout ── */
#main { display: flex; flex: 1; overflow: hidden; min-height: 0; }

/* ── Graph pane ── */
#graph-pane { flex: 1; position: relative; min-width: 0; }
#mynetwork  { width: 100%; height: 100%; background: #0d0d1a; }

/* ── Divider ── */
#divider {
  width: 5px; background: #1a2a4a;
  cursor: col-resize; flex-shrink: 0;
  transition: background 0.15s;
}
#divider:hover { background: #3a5a8a; }

/* ── Chart pane ── */
#chart-pane {
  width: 440px; min-width: 440px;
  background: #12122a;
  border-left: 1px solid #2d4a7a;
  display: flex; flex-direction: column;
}
#chart-header {
  padding: 8px 14px;
  border-bottom: 1px solid #2d4a7a;
  background: #16213e;
  flex-shrink: 0;
}
#chart-title    { font-size: 13px; font-weight: bold; color: #7eb8f7; margin-bottom: 2px; }
#chart-subtitle { font-size: 11px; color: #888; }
#chart-body          { flex: 1; display: flex; flex-direction: column; min-height: 0; }
.chart-sub-pane      { flex: 1; display: flex; flex-direction: column; min-height: 0; }
.chart-sub-label     { font-size: 10px; font-weight: bold; color: #7eb8f7;
                        text-transform: uppercase; letter-spacing: 0.07em;
                        padding: 3px 10px; background: #0e0e1f;
                        border-bottom: 1px solid #1a2a4a; flex-shrink: 0; }
#chart-plotly        { width: 100%; flex: 1; min-height: 0; display: none; }
#chart-plotly-current{ width: 100%; flex: 1; min-height: 0; display: none; }
.chart-sub-ph        { flex: 1; display: flex; align-items: center; justify-content: center;
                        color: #444; font-size: 11px; text-align: center;
                        padding: 12px; line-height: 1.6; }
#chart-hdivider      { height: 5px; background: #1a2a4a; cursor: row-resize; flex-shrink: 0;
                        transition: background 0.15s; }
#chart-hdivider:hover { background: #3a5a8a; }

/* ── Legend ── */
#legend {
  position: absolute; top: 12px; left: 12px; z-index: 9999;
  background: rgba(13,13,26,0.93);
  border: 1px solid #2d4a7a;
  border-radius: 8px; padding: 10px 14px;
  color: #e0e0e0; font-size: 11px; line-height: 1.9;
  pointer-events: none;
}
#legend b { font-size: 12px; color: #7eb8f7; }

/* ── vis tooltip ── */
.vis-tooltip {
  white-space: pre-line !important;
  font-family: monospace !important;
  font-size: 11px !important;
  background: #1e1e2e !important;
  color: #e0e0e0 !important;
  border: 1px solid #555 !important;
  border-radius: 6px !important;
  padding: 8px 10px !important;
  max-width: 380px !important;
}
</style>
</head>
<body>

<div id="header">
  <h1>&#9889; Grid Simulation — Live Dashboard</h1>
  <div id="status-badge">
    <div id="status-dot"></div>
    <span id="status-text">LIVE</span>
  </div>
  <div id="progress-wrap">
    <div id="progress-bar"></div>
  </div>
  <span id="progress-label"></span>
  <span id="data-keys" style="font-size:10px;color:#888;margin-left:auto;">⏳ waiting for data</span>
</div>

<div id="main">
  <div id="graph-pane">
    <div id="mynetwork"></div>
    <div id="legend">
      <b>CIGRE MV Network</b><br>
      <span style="display:inline-block;width:12px;height:12px;border-radius:50%;background:#4C9BE8;vertical-align:middle;margin-right:6px;"></span>Substation (V, &#952;)<br>
      <span style="display:inline-block;width:12px;height:12px;background:#2ECC71;vertical-align:middle;margin-right:6px;"></span>Transformer (V<sub style="font-size:9px">LV</sub>)<br>
      <span style="display:inline-block;width:0;height:0;border-left:6px solid transparent;border-right:6px solid transparent;border-bottom:12px solid #E8A838;vertical-align:middle;margin-right:6px;"></span>Load<br>
      <span style="display:inline-block;width:36px;height:2px;background:#F4A261;vertical-align:middle;margin-right:6px;"></span>Line / TR (P, Q loss)<br>
      <span style="display:inline-block;width:36px;height:0;border-top:2px dashed #888;vertical-align:middle;margin-right:6px;"></span>Load connection<br>
      <span style="color:#555;font-size:10px;">Click to pin chart</span>
    </div>
  </div>

  <div id="divider"></div>

  <div id="chart-pane">
    <div id="chart-header">
      <div id="chart-title">Simulation Results</div>
      <div id="chart-subtitle">Hover or click a node / edge in the network</div>
    </div>
    <div id="chart-body">
      <div class="chart-sub-pane" id="chart-power-pane">
        <div class="chart-sub-label" id="label-power">&#9889; Branch Power &mdash; P &amp; Q Loss</div>
        <div id="chart-plotly"></div>
        <div class="chart-sub-ph" id="chart-power-ph">
          Hover or click a <span style="color:#F4A261;margin:0 3px;">line edge</span>
          to view power loss
        </div>
      </div>
      <div id="chart-hdivider"></div>
      <div class="chart-sub-pane" id="chart-current-pane">
        <div class="chart-sub-label" id="label-current">&#126; Branch Current &mdash; I from (kA)</div>
        <div id="chart-plotly-current"></div>
        <div class="chart-sub-ph" id="chart-current-ph">
          Hover or click a <span style="color:#9B59B6;margin:0 3px;">line edge</span>
          to view branch current
        </div>
      </div>
    </div>
  </div>
</div>

<script>
// ============================================================
// Network graph (embedded from graph.html)
// ============================================================
var nodes   = new vis.DataSet(NODES_PLACEHOLDER);
var edges   = new vis.DataSet(EDGES_PLACEHOLDER);
var netOpts = OPTIONS_PLACEHOLDER;
netOpts.interaction = netOpts.interaction || {};
netOpts.interaction.hover          = true;
netOpts.interaction.tooltipDelay   = 80;
netOpts.interaction.navigationButtons = true;
netOpts.interaction.keyboard       = true;

var network   = new vis.Network(document.getElementById("mynetwork"), { nodes, edges }, netOpts);
var nodeColors = {};
network.on("afterDrawing", function() {
  if (Object.keys(nodeColors).length) return;
  var all = nodes.get({ returnType: "Object" });
  for (var id in all) nodeColors[id] = all[id].color;
});

var highlightActive = false;
function highlightNeighbourhood(nodeId) {
  var all = nodes.get({ returnType: "Object" });
  highlightActive = true;
  for (var id in all) {
    all[id].color = "rgba(50,50,70,0.3)";
    if (all[id].hiddenLabel === undefined) { all[id].hiddenLabel = all[id].label; all[id].label = undefined; }
  }
  network.getConnectedNodes(nodeId).concat([nodeId]).forEach(function(id) {
    all[id].color = nodeColors[id];
    if (all[id].hiddenLabel !== undefined) { all[id].label = all[id].hiddenLabel; delete all[id].hiddenLabel; }
  });
  nodes.update(Object.values(all));
}
function resetHighlight() {
  if (!highlightActive) return;
  var all = nodes.get({ returnType: "Object" });
  for (var id in all) {
    all[id].color = nodeColors[id];
    if (all[id].hiddenLabel !== undefined) { all[id].label = all[id].hiddenLabel; delete all[id].hiddenLabel; }
  }
  nodes.update(Object.values(all));
  highlightActive = false;
}
network.on("selectNode",   function(p) { highlightNeighbourhood(p.nodes[0]); });
network.on("deselectNode", resetHighlight);

// ============================================================
// Simulation time mapping — step → real wall-clock time
// start_time is read from sim_status.json (set when simulation starts)
// ============================================================
var SIM_START_DATE    = new Date(); // placeholder; overwritten from sim_status
var STEP_DURATION_SEC = 1; // 1 second per mosaik step (time_resolution=1.0, step_size=1)

// ============================================================
// Live data (polled from server)
// ============================================================
var outputData = {};
var outputDataLoaded = false;
var simStatus  = { running: true, step: 0, total: null };

// Tracks what is currently displayed in the chart pane.
// { type: 'node'|'edge', id: string } or null
var currentView = null;
var pinned      = false;

var _fetchingData   = false;
var _fetchingStatus = false;

async function refreshData() {
  if (_fetchingData) return;
  _fetchingData = true;
  try {
    var r = await fetch("./output.json?t=" + Date.now());
    if (r.ok) {
      var text = await r.text();
      outputData = JSON.parse(text);
      outputDataLoaded = Object.keys(outputData).length > 0;
      var kEl = document.getElementById('data-keys');
      if (kEl) kEl.textContent = outputDataLoaded
        ? '\u2713 ' + Object.keys(outputData).length + ' series loaded'
        : '\u26a0\ufe0f no data in file';
      // Re-render whatever is currently shown so the chart updates live
      _rerenderCurrentView();
    } else {
      var kEl = document.getElementById('data-keys');
      if (kEl) kEl.textContent = '\u23f3 waiting for data\u2026 (HTTP ' + r.status + ')';
    }
  } catch(err) {
    console.error('[Dashboard] output.json fetch/parse failed:', err);
    var kEl = document.getElementById('data-keys');
    if (kEl) kEl.textContent = '\u274c ' + (err.message || String(err));
  }
  _fetchingData = false;
}

async function refreshStatus() {
  if (_fetchingStatus) return;
  _fetchingStatus = true;
  try {
    var r = await fetch("./sim_status.json?t=" + Date.now());
    if (r.ok) {
      simStatus = await r.json();
      // Update start time from the real wall-clock timestamp recorded in sim_status.json
      if (simStatus.start_time) {
        SIM_START_DATE = new Date(simStatus.start_time);
      }
      updateStatusUI();
    }
  } catch(_) {}
  _fetchingStatus = false;
}

function updateStatusUI() {
  var badge    = document.getElementById("status-badge");
  var dot      = document.getElementById("status-dot");
  var text     = document.getElementById("status-text");
  var bar      = document.getElementById("progress-bar");
  var label    = document.getElementById("progress-label");
  var step     = simStatus.step  || 0;
  var total    = simStatus.total || "?";
  var running  = simStatus.running;

  badge.className = running ? "" : "done";
  text.textContent = running ? "LIVE" : "COMPLETED";

  if (typeof total === "number" && total > 0) {
    bar.style.width = Math.min(100, Math.round(step / total * 100)) + "%";
  } else {
    bar.style.width = "0%";
  }
}

var DATA_INTERVAL   = setInterval(refreshData,   500);
var STATUS_INTERVAL = setInterval(refreshStatus, 500);

// Live system clock — updates the progress label every second
function pad(n) { return n < 10 ? '0' + n : '' + n; }
function formatNow() {
  var d = new Date();
  return pad(d.getDate()) + '.' + pad(d.getMonth() + 1) + '.' + d.getFullYear() +
         ' ' + pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds());
}
function tickClock() {
  var label = document.getElementById('progress-label');
  if (label) label.textContent = formatNow();
}
tickClock();
setInterval(tickClock, 1000);

// Initial fetch
refreshData();
refreshStatus();

// ============================================================
// Chart helpers (identical to integrated.html)
// ============================================================
function buildBusFragment(label) {
  var m = (label || "").match(/BUS_(\d+)\s+([\d.]+)/);
  if (!m) return null;
  var pad = "_".repeat(Math.max(0, 4 - m[1].length));
  return "bus_" + m[1] + pad + m[2];
}

function getNodeSeries(nodeId) {
  var node  = nodes.get(nodeId);
  if (!node) return null;
  var label = node.label || "";
  var title = node.title || "";
  var tm    = title.match(/^\[(\w+)\]/);
  var ntype = tm ? tm[1] : "";

  if (ntype === "Substation") {
    var frag = buildBusFragment(label);
    if (!frag) return null;
    var series = [];
    if (outputData["V_sub_" + frag + "_mag_kv"])
      series.push({ key: "V_sub_" + frag + "_mag_kv", name: "Voltage (kV)", color: "#4C9BE8", yaxis: "y"  });
    if (outputData["V_sub_" + frag + "_ang_deg"])
      series.push({ key: "V_sub_" + frag + "_ang_deg", name: "Angle (deg)",  color: "#2ECC71", yaxis: "y3" });
    if (!series.length) {
      return { title: label.trim(),
               subtitle: outputDataLoaded ? 'Key not found in output.json — check bus name mapping' : 'Simulation data loading\u2026',
               series: [] };
    }
    return { title: label.trim(), subtitle: "Bus Voltage Magnitude & Angle", type: 'substation', series: series };

  } else if (ntype === "Transformer") {
    var bm = label.match(/BRANCH-(\d+)/);
    if (!bm) return null;
    var key = "V_branch_" + bm[1] + "_LV";
    if (!outputData[key]) {
      return { title: label.trim(),
               subtitle: outputDataLoaded ? 'Key \"' + key + '\" not in output.json' : 'Simulation data loading\u2026',
               series: [] };
    }
    return { title: label.trim(), subtitle: "LV-side Voltage (kV)", type: 'transformer',
             series: [{ key: key, name: "V_LV (kV)", color: "#2ECC71", yaxis: "y" }] };

  } else if (ntype === "Load") {
    return { title: label.trim(), subtitle: "Load \u2014 no time-series output available", series: [] };
  }
  return null;
}

function getEdgeSeries(edgeId) {
  var edge = edges.get(edgeId);
  if (!edge || edge.dashes) return null;
  var fn = nodes.get(edge.from);
  var tn = nodes.get(edge.to);
  if (!fn || !tn) return null;
  var ff = buildBusFragment(fn.label || "");
  var tf = buildBusFragment(tn.label || "");
  if (!ff || !tf) return null;

  var pKey = "P_loss_line_" + ff + "_" + tf + "_mw";
  var qKey = "Q_loss_line_" + ff + "_" + tf + "_mvar";
  var iKey = "I_from_line_" + ff + "_" + tf + "_kA";
  if (!outputData[pKey]) {
    pKey = "P_loss_line_" + tf + "_" + ff + "_mw";
    qKey = "Q_loss_line_" + tf + "_" + ff + "_mvar";
    iKey = "I_from_line_" + tf + "_" + ff + "_kA";
  }
  var series = [];
  if (outputData[pKey]) series.push({ key: pKey, name: "P Loss (MW)",   color: "#FF8C00", yaxis: "y",  mode: "lines" });
  if (outputData[qKey]) series.push({ key: qKey, name: "Q Loss (Mvar)", color: "#4C9BE8", yaxis: "y2", mode: "lines" });
  if (outputData[iKey]) series.push({ key: iKey, name: "I from (kA)",   color: "#9B59B6", yaxis: "y3" });
  if (!series.length) {
    return {
      title:    (edge.label || "Line").trim(),
      subtitle: outputDataLoaded
        ? 'No loss keys found for ' + (fn.label||'').trim() + ' \u2192 ' + (tn.label||'').trim()
        : 'Simulation data loading\u2026',
      series:   []
    };
  }

  return {
    title:    (edge.label || "Line").trim(),
    subtitle: (fn.label || "").trim() + " \u2192 " + (tn.label || "").trim(),
    type:     'edge',
    series:   series
  };
}

function _buildLayout(traces, hasY2) {
  var y2trace = traces.find(function(t) { return t && t.yaxis === "y2"; });
  var layout = {
    paper_bgcolor: "#12122a", plot_bgcolor: "#12122a",
    font: { color: "#c0c0d0", size: 11 },
    margin: { l: 52, r: hasY2 ? 52 : 16, t: 14, b: 36 },
    xaxis: { title: { text: "Time", font: { size: 11 } },
             type: "date", tickformat: "%d.%m.%Y %H:%M:%S",
             gridcolor: "#1e2040", zerolinecolor: "#2d4a7a", tickfont: { size: 10 } },
    yaxis: { title: { text: traces[0] ? traces[0].name : "", font: { size: 11 } },
             gridcolor: "#1e2040", zerolinecolor: "#2d4a7a", tickfont: { size: 10 } },
    legend: { x: 0.01, y: 0.99, bgcolor: "rgba(10,10,30,0.7)",
              font: { size: 10 }, bordercolor: "#2d4a7a", borderwidth: 1 },
    hovermode: "x unified",
    hoverlabel: { bgcolor: "#1e2040", bordercolor: "#2d4a7a", font: { size: 11 } }
  };
  if (hasY2) {
    layout.yaxis2 = {
      title: { text: y2trace ? y2trace.name : "", font: { size: 11 } },
      overlaying: "y", side: "right",
      gridcolor: "#1a1a30", zeroline: false, tickfont: { size: 10 }
    };
  }
  return layout;
}

function renderChart(chartData, isPinned) {
  var titleEl    = document.getElementById("chart-title");
  var subEl      = document.getElementById("chart-subtitle");
  var plotDiv    = document.getElementById("chart-plotly");
  var plotDiv2   = document.getElementById("chart-plotly-current");
  var powerPh    = document.getElementById("chart-power-ph");
  var currentPh  = document.getElementById("chart-current-ph");
  var labelPower   = document.getElementById("label-power");
  var labelCurrent = document.getElementById("label-current");

  function showPowerPh(msg)    { plotDiv.style.display   = "none";  powerPh.style.display   = "flex"; if (msg) powerPh.innerHTML   = msg; }
  function showCurrentPh(msg)  { plotDiv2.style.display  = "none";  currentPh.style.display = "flex"; if (msg) currentPh.innerHTML = msg; }
  function showPowerChart()    { plotDiv.style.display   = "block"; powerPh.style.display   = "none"; }
  function showCurrentChart()  { plotDiv2.style.display  = "block"; currentPh.style.display = "none"; }

  if (!chartData) {
    titleEl.textContent = "Simulation Results";
    subEl.innerHTML     = "Hover or click a node / edge in the network";
    labelPower.innerHTML   = "&#9889; Branch Power &mdash; P &amp; Q Loss";
    labelCurrent.innerHTML = "&#126; Branch Current &mdash; I from (kA)";
    showPowerPh("Hover or click a <span style='color:#F4A261;margin:0 3px;'>line edge</span> to view power loss");
    showCurrentPh("Hover or click a <span style='color:#9B59B6;margin:0 3px;'>line edge</span> to view branch current");
    return;
  }

  // Update sub-pane labels based on element type
  if (chartData.type === 'substation') {
    labelPower.innerHTML   = "&#9889; Voltage Magnitude (kV)";
    labelCurrent.innerHTML = "&#126; Voltage Angle (deg)";
  } else if (chartData.type === 'transformer') {
    labelPower.innerHTML   = "&#9889; LV-side Voltage (kV)";
    labelCurrent.innerHTML = "&#126; Branch Current &mdash; I from (kA)";
  } else {
    labelPower.innerHTML   = "&#9889; Branch Power &mdash; P &amp; Q Loss";
    labelCurrent.innerHTML = "&#126; Branch Current &mdash; I from (kA)";
  }

  titleEl.textContent = chartData.title;
  subEl.innerHTML     = (isPinned ? "<span style='color:#f4a261;'>&#128204; </span>" : "") + chartData.subtitle;

  // Separate power (y / y2) series from current (y3) series
  var powerSeries   = chartData.series.filter(function(s) { return s.yaxis !== "y3"; });
  var currentSeries = chartData.series.filter(function(s) { return s.yaxis === "y3"; });

  function seriesToTraces(seriesList, remapToY) {
    return seriesList.map(function(s) {
      var sd = outputData[s.key];
      if (!sd) return null;
      var steps = Object.keys(sd).map(Number).sort(function(a,b){return a-b;});
      var yaxis = remapToY ? "y" : (s.yaxis || "y");
      var mode  = s.mode || "lines+markers";
      return {
        x: steps.map(function(t){ return new Date(SIM_START_DATE.getTime() + t * STEP_DURATION_SEC * 1000).toISOString(); }),
        y: steps.map(function(t){ return sd[String(t)]; }),
        name: s.name, type: "scatter", mode: mode,
        marker: { size: 5, color: s.color },
        line:   { color: s.color, width: 2 },
        yaxis:  yaxis
      };
    }).filter(Boolean);
  }

  var powerTraces   = seriesToTraces(powerSeries, false);
  var currentTraces = seriesToTraces(currentSeries, true);

  // ── Power sub-pane ──
  if (powerTraces.length) {
    var hasY2 = powerSeries.some(function(s) { return s.yaxis === "y2"; });
    showPowerChart();
    try {
      Plotly.react("chart-plotly", powerTraces, _buildLayout(powerTraces, hasY2), { responsive: true, displayModeBar: false });
    } catch(e) { console.error('[Dashboard] Plotly.react (power) failed:', e); }
  } else {
    showPowerPh(chartData.series.length
      ? "<span style='color:#666;'>No power data for this element</span>"
      : "Hover or click a <span style='color:#F4A261;margin:0 3px;'>line edge</span> to view power loss");
  }

  // ── Current sub-pane ──
  if (currentTraces.length) {
    showCurrentChart();
    try {
      Plotly.react("chart-plotly-current", currentTraces, _buildLayout(currentTraces, false), { responsive: true, displayModeBar: false });
    } catch(e) { console.error('[Dashboard] Plotly.react (current) failed:', e); }
  } else {
    showCurrentPh(chartData.series.length
      ? "<span style='color:#666;'>No current data for this element</span>"
      : "Hover or click a <span style='color:#9B59B6;margin:0 3px;'>line edge</span> to view branch current");
  }
}

// ============================================================
// Hover / click events
// ============================================================
var hoverTimer  = null;

function _getChartForView(view) {
  if (!view) return null;
  return view.type === 'node' ? getNodeSeries(view.id) : getEdgeSeries(view.id);
}

function _rerenderCurrentView() {
  if (!currentView) return;
  renderChart(_getChartForView(currentView), pinned);
}

network.on("hoverNode", function(p) {
  if (pinned) return;
  clearTimeout(hoverTimer);
  hoverTimer = setTimeout(function() {
    currentView = { type: 'node', id: p.node };
    renderChart(getNodeSeries(p.node), false);
  }, 80);
});
network.on("hoverEdge", function(p) {
  if (pinned) return;
  clearTimeout(hoverTimer);
  hoverTimer = setTimeout(function() {
    currentView = { type: 'edge', id: p.edge };
    renderChart(getEdgeSeries(p.edge), false);
  }, 80);
});
network.on("blurNode",  function() {
  if (pinned) return;
  clearTimeout(hoverTimer);
  // keep showing the chart until something else takes over
});
network.on("blurEdge",  function() {
  if (pinned) return;
  clearTimeout(hoverTimer);
});

network.on("click", function(p) {
  clearTimeout(hoverTimer);

  if (p.nodes.length > 0) {
    currentView = { type: 'node', id: p.nodes[0] };
    var d = getNodeSeries(p.nodes[0]);
    if (d && d.series && d.series.length > 0) {
      pinned = true;
    } else {
      pinned = false;
    }
    renderChart(d, pinned);
  } else if (p.edges.length > 0) {
    currentView = { type: 'edge', id: p.edges[0] };
    var d = getEdgeSeries(p.edges[0]);
    if (d && d.series && d.series.length > 0) {
      pinned = true;
    } else {
      pinned = false;
    }
    renderChart(d, pinned);
  } else {
    // Click on empty canvas — unpin
    pinned      = false;
    currentView = null;
    renderChart(null, false);
  }
});

// ============================================================
// Resizable dividers
// ============================================================
// Left/right: chart pane width
(function() {
  var divider = document.getElementById("divider");
  var pane    = document.getElementById("chart-pane");
  var drag    = false;
  divider.addEventListener("mousedown", function(e) { drag = true; document.body.style.cursor = "col-resize"; e.preventDefault(); });
  document.addEventListener("mousemove", function(e) {
    if (!drag) return;
    var r = document.getElementById("main").getBoundingClientRect();
    var w = Math.max(300, Math.min(700, r.right - e.clientX));
    pane.style.width = pane.style.minWidth = w + "px";
  });
  document.addEventListener("mouseup", function() { drag = false; document.body.style.cursor = ""; });
})();
// Top/bottom: power vs current chart split
(function() {
  var hdivider   = document.getElementById("chart-hdivider");
  var topPane    = document.getElementById("chart-power-pane");
  var botPane    = document.getElementById("chart-current-pane");
  var drag       = false;
  hdivider.addEventListener("mousedown", function(e) { drag = true; document.body.style.cursor = "row-resize"; e.preventDefault(); });
  document.addEventListener("mousemove", function(e) {
    if (!drag) return;
    var body = document.getElementById("chart-body").getBoundingClientRect();
    var relY = e.clientY - body.top;
    var topH = Math.max(80, Math.min(body.height - 80 - 5, relY));
    topPane.style.flex = "none";
    botPane.style.flex = "none";
    topPane.style.height = topH + "px";
    botPane.style.height = (body.height - topH - 5) + "px";
  });
  document.addEventListener("mouseup", function() { drag = false; document.body.style.cursor = ""; });
})();
</script>
</body>
</html>
"""
