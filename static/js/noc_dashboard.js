"use strict";

/**
 * Cliente "Cyber-NOC Tactical Command Center".
 *
 * - Consume los endpoints REST (/api/config, /api/topology, /api/events,
 *   /api/telemetry/latest, /api/simulate/*) y el canal /ws/telemetry.
 * - Renderiza el radar de topología con Cytoscape.js e iconos SVG por rol.
 * - Inspector HUD con barras segmentadas y gráficas Chart.js con degradados.
 * - Dock de inyección de fallas (interruptores tácticos) y terminal de eventos.
 */

// ---------------------------------------------------------------------------
// Constantes de identidad visual y operación
// ---------------------------------------------------------------------------

const STATUS_COLORS = Object.freeze({ up: "#00f59b", degraded: "#ffb703", down: "#ff2a5f" });
const STATUS_LABELS = Object.freeze({ up: "ONLINE", degraded: "DEGRADED", down: "CRITICAL" });
const ACCENTS = Object.freeze({ cyan: "#06b6d4", violet: "#8b5cf6", magenta: "#d946ef" });
const ROLE_RANK = Object.freeze({ core: 3, distribution: 2, edge: 1 });
const ROLE_LABELS = Object.freeze({
  core: "CORE ROUTER",
  distribution: "DISTRIBUTION NODE",
  edge: "EDGE GATEWAY",
});
const SEVERITY_BY_KIND = Object.freeze({ down: "CRIT", degraded: "WARN", recovered: "OK" });

const MAX_SAMPLES = 40;
const MAX_LOG_LINES = 100;
const BAR_SEGMENTS = 20;
const LATENCY_SCALE_MS = 200;
// Umbrales por defecto del backend (LATENCY_DEGRADED_MS / CPU_DEGRADED_PERCENT).
const LATENCY_WARN_MS = 150;
const CPU_WARN_PERCENT = 85;
const BASE_RECONNECT_MS = 1000;
const MAX_RECONNECT_MS = 10000;
const MONO_FONT = '"JetBrains Mono", ui-monospace, monospace';
const REDUCED_MOTION = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

const state = {
  simulation: false,
  pollInterval: 3,
  topology: { nodes: [], edges: [] },
  latestSnapshot: null,
  selectedNodeId: null,
  historyByNode: new Map(),
  forcedNodes: new Set(),
  forcedEdges: new Set(),
  simControls: new Map(),
  quickChips: new Map(),
  logKeys: new Set(),
  cy: null,
  bwChart: null,
  cpuChart: null,
  ws: null,
  wsConnected: false,
  reconnectDelayMs: BASE_RECONNECT_MS,
  reconnectTimer: null,
};

const $ = (id) => document.getElementById(id);

// ---------------------------------------------------------------------------
// Utilidades de formato
// ---------------------------------------------------------------------------

function formatUtc(isoTimestamp) {
  const date = isoTimestamp ? new Date(isoTimestamp) : new Date();
  if (Number.isNaN(date.getTime())) return "--:--:--";
  return date.toISOString().slice(11, 19);
}

function formatNum(value, unit = "", digits = 1) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "N/D";
  return `${Number(value).toFixed(digits)}${unit}`;
}

function formatKbps(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "N/D";
  return `${Math.round(value).toLocaleString("es-MX")} Kbps`;
}

function shortIf(name) {
  return String(name || "").replace("GigabitEthernet", "Gi").replace("FastEthernet", "Fa");
}

// ---------------------------------------------------------------------------
// Iconos SVG técnicos por rol (data URI para Cytoscape e inspector)
// ---------------------------------------------------------------------------

const ICON_CACHE = new Map();

function iconMarker(color) {
  return (
    "<defs><marker id='h' viewBox='0 0 6 6' refX='4.5' refY='3' markerWidth='4' markerHeight='4' " +
    `orient='auto-start-reverse'><path d='M0 0L6 3L0 6z' fill='${color}' stroke='none'/></marker></defs>`
  );
}

const ICON_BODIES = Object.freeze({
  // Core Router: chasis cilíndrico, 4 flechas cruzadas y halo doble.
  core: () =>
    "<circle cx='32' cy='32' r='30' stroke-opacity='0.35'/>" +
    "<circle cx='32' cy='32' r='26.5' stroke-opacity='0.6' stroke-dasharray='2 3.5'/>" +
    "<ellipse cx='32' cy='22' rx='14' ry='4.5'/>" +
    "<path d='M18 22v19c0 2.5 6.3 4.5 14 4.5s14-2 14-4.5V22'/>" +
    "<line x1='24.5' y1='29' x2='39.5' y2='39' marker-start='url(#h)' marker-end='url(#h)'/>" +
    "<line x1='39.5' y1='29' x2='24.5' y2='39' marker-start='url(#h)' marker-end='url(#h)'/>",
  // Distribution Node: chasis multicapa con flechas bidireccionales.
  distribution: (c) =>
    "<rect x='11' y='14' width='42' height='11' rx='2.5'/>" +
    "<rect x='11' y='27' width='42' height='10' rx='2.5' stroke-opacity='0.7'/>" +
    "<rect x='11' y='39' width='42' height='11' rx='2.5'/>" +
    "<line x1='19' y1='19.5' x2='45' y2='19.5' marker-start='url(#h)' marker-end='url(#h)'/>" +
    "<line x1='19' y1='44.5' x2='45' y2='44.5' marker-start='url(#h)' marker-end='url(#h)'/>" +
    `<circle cx='17' cy='32' r='1.4' fill='${c}' stroke='none'/>` +
    `<circle cx='22' cy='32' r='1.4' fill='${c}' stroke='none'/>` +
    `<circle cx='27' cy='32' r='1.4' fill='${c}' stroke='none'/>` +
    "<line x1='38' y1='32' x2='48' y2='32' stroke-opacity='0.7'/>",
  // Edge Gateway: nodo perimetral con antena y enlace externo.
  edge: (c) =>
    "<rect x='9' y='34' width='35' height='15' rx='3'/>" +
    `<circle cx='16' cy='41.5' r='1.6' fill='${c}' stroke='none'/>` +
    `<circle cx='22' cy='41.5' r='1.6' fill='${c}' stroke='none'/>` +
    "<line x1='28' y1='41.5' x2='36' y2='41.5' stroke-opacity='0.7'/>" +
    "<line x1='37' y1='34' x2='37' y2='18'/>" +
    `<circle cx='37' cy='16' r='2' fill='${c}' stroke='none'/>` +
    "<path d='M31 12.5a8.5 8.5 0 0 1 12 0M27.5 9a13.5 13.5 0 0 1 19 0' stroke-opacity='0.8'/>" +
    "<line x1='44' y1='41.5' x2='57' y2='41.5' stroke-dasharray='2.5 2.5' marker-end='url(#h)'/>",
  // Switch genérico (tipo switch sin rol conocido).
  switch: () =>
    "<rect x='8' y='20' width='48' height='24' rx='4'/>" +
    "<line x1='16' y1='28' x2='30' y2='28' marker-end='url(#h)'/>" +
    "<line x1='48' y1='28' x2='34' y2='28' marker-end='url(#h)'/>" +
    "<line x1='30' y1='36' x2='16' y2='36' marker-end='url(#h)'/>" +
    "<line x1='34' y1='36' x2='48' y2='36' marker-end='url(#h)'/>",
  // Servidor genérico.
  server: (c) =>
    "<rect x='16' y='10' width='32' height='14' rx='2.5'/>" +
    "<rect x='16' y='26' width='32' height='14' rx='2.5'/>" +
    "<rect x='16' y='42' width='32' height='12' rx='2.5' stroke-opacity='0.7'/>" +
    `<circle cx='22' cy='17' r='1.5' fill='${c}' stroke='none'/>` +
    `<circle cx='22' cy='33' r='1.5' fill='${c}' stroke='none'/>` +
    "<line x1='30' y1='17' x2='42' y2='17'/><line x1='30' y1='33' x2='42' y2='33'/>",
});

function getNodeIcon(role, type, color) {
  const key = `${role}|${type}|${color}`;
  const cached = ICON_CACHE.get(key);
  if (cached) return cached;

  const builder = ICON_BODIES[role] || ICON_BODIES[type] || ICON_BODIES.core;
  const svg =
    "<svg xmlns='http://www.w3.org/2000/svg' width='64' height='64' viewBox='0 0 64 64' fill='none' " +
    `stroke='${color}' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'>` +
    `${iconMarker(color)}${builder(color)}</svg>`;
  const uri = `data:image/svg+xml;utf8,${encodeURIComponent(svg)}`;
  ICON_CACHE.set(key, uri);
  return uri;
}

// ---------------------------------------------------------------------------
// Radar de topología (Cytoscape.js)
// ---------------------------------------------------------------------------

function nodeShape(role) {
  return role === "core" ? "round-hexagon" : "round-rectangle";
}

function nodeLabel(name, ip, metric) {
  if (!metric) return `${name}\n${ip}`;
  if (metric.status === "down") return `${name}\n${ip} · SIN RESPUESTA`;
  return `${name}\n${ip} · ${formatNum(metric.latency_ms, " ms")}`;
}

function edgeLabel(baseIf, metric) {
  if (!metric) return baseIf;
  if (metric.status === "down") return `${baseIf}\n✕ LINK DOWN`;
  const traffic = `▲ ${formatKbps(metric.bw_in_kbps)}  ▼ ${formatKbps(metric.bw_out_kbps)}`;
  return metric.status === "degraded" ? `${baseIf}\n⚠ ${traffic}` : `${baseIf}\n${traffic}`;
}

function buildCyStyle() {
  return [
    {
      selector: "node",
      style: {
        shape: "data(shape)",
        width: 78,
        height: 78,
        "background-color": "#081120",
        "background-opacity": 0.94,
        "background-image": "data(icon)",
        "background-fit": "none",
        "background-width": "68%",
        "background-height": "68%",
        "background-clip": "none",
        "border-width": 2.5,
        "border-color": "data(color)",
        "underlay-color": "data(color)",
        "underlay-padding": 10,
        "underlay-opacity": 0.16,
        "underlay-shape": "ellipse",
        label: "data(label)",
        color: "#e2f8ff",
        "font-family": MONO_FONT,
        "font-size": 10.5,
        "font-weight": 500,
        "text-valign": "bottom",
        "text-halign": "center",
        "text-margin-y": 8,
        "text-wrap": "wrap",
        "text-background-color": "#05080f",
        "text-background-opacity": 0.88,
        "text-background-padding": 4,
        "text-background-shape": "round-rectangle",
        "text-border-color": ACCENTS.cyan,
        "text-border-width": 1,
        "text-border-opacity": 0.25,
        "z-index": 10,
        "transition-property": "border-color, underlay-color, background-color, opacity",
        "transition-duration": "0.35s",
      },
    },
    { selector: 'node[status = "degraded"]', style: { "underlay-opacity": 0.24 } },
    {
      selector: 'node[status = "down"]',
      style: {
        "border-style": "dashed",
        "border-width": 3,
        "underlay-opacity": 0.3,
        "background-color": "#1a0610",
        color: "#ffd1dc",
      },
    },
    {
      selector: "node:selected",
      style: {
        "overlay-color": ACCENTS.cyan,
        "overlay-opacity": 0.1,
        "overlay-padding": 14,
        "overlay-shape": "round-rectangle",
        "text-border-opacity": 0.85,
      },
    },
    {
      selector: "edge",
      style: {
        width: 3,
        "curve-style": "bezier",
        "line-color": "data(color)",
        "line-cap": "round",
        "underlay-color": "data(color)",
        "underlay-padding": 5,
        "underlay-opacity": 0.14,
        label: "",
        "transition-property": "line-color, underlay-color, opacity",
        "transition-duration": "0.35s",
      },
    },
    { selector: 'edge[status = "degraded"]', style: { "line-style": "dashed", "line-dash-pattern": [2, 6] } },
    {
      selector: 'edge[status = "down"]',
      style: {
        "line-style": "dashed",
        "line-dash-pattern": [10, 7],
        width: 3.5,
        "underlay-opacity": 0.28,
        color: "#ffd1dc",
      },
    },
    { selector: ".dimmed", style: { opacity: 0.28 } },
  ];
}

function initCytoscape(topology) {
  const container = $("cy");
  if (!container || typeof window.cytoscape !== "function") return;

  const elements = [];
  for (const node of topology.nodes) {
    elements.push({
      group: "nodes",
      data: {
        id: node.id,
        name: node.name,
        ip: node.ip,
        type: node.type,
        role: node.role,
        status: "up",
        color: STATUS_COLORS.up,
        shape: nodeShape(node.role),
        icon: getNodeIcon(node.role, node.type, STATUS_COLORS.up),
        label: nodeLabel(node.name, node.ip, null),
      },
      position: { x: node.position?.x ?? 0, y: node.position?.y ?? 0 },
    });
  }

  for (const edge of topology.edges) {
    const baseIf = `${shortIf(edge.source_if?.name)} ⇄ ${shortIf(edge.target_if?.name)}`;
    elements.push({
      group: "edges",
      data: {
        id: edge.id,
        source: edge.source,
        target: edge.target,
        baseIf,
        status: "up",
        bwIn: 0,
        bwOut: 0,
        color: STATUS_COLORS.up,
      },
    });
  }

  state.cy = window.cytoscape({
    container,
    elements,
    layout: {
      name: "breadthfirst",
      directed: true,
      roots: '[role = "core"]',
      spacingFactor: 1.75,
      padding: 80,
    },
    minZoom: 0.4,
    maxZoom: 2.5,
    boxSelectionEnabled: false,
    style: buildCyStyle(),
  });

  state.cy.on("tap", "node", (event) => selectNode(event.target.id()));
  state.cy.on("tap", (event) => {
    if (event.target === state.cy && state.selectedNodeId) clearSelection();
  });

  const tooltip = $("edge-tooltip");
  const positionTooltip = (evt) => {
    if (!tooltip) return;
    const clientX = evt.originalEvent?.clientX ?? 0;
    const clientY = evt.originalEvent?.clientY ?? 0;
    const offset = 12;
    const rect = tooltip.getBoundingClientRect();
    let left = clientX + offset;
    let top = clientY + offset;
    if (left + rect.width > window.innerWidth - 8) {
      left = Math.max(8, clientX - rect.width - offset);
    }
    if (top + rect.height > window.innerHeight - 8) {
      top = Math.max(8, clientY - rect.height - offset);
    }
    tooltip.style.left = `${left}px`;
    tooltip.style.top = `${top}px`;
  };
  state.cy.on("mouseover", "edge", (event) => {
    if (!tooltip) return;
    const d = event.target.data();
    const st = String(d.status || "up").toUpperCase();
    const color = STATUS_COLORS[d.status] ?? STATUS_COLORS.up;
    const inVal = formatNum(d.bwIn ?? 0, "", 1);
    const outVal = formatNum(d.bwOut ?? 0, "", 1);
    tooltip.replaceChildren();
    const title = document.createElement("div");
    title.className = "font-semibold text-cyan-300";
    title.textContent = `${d.id} · ${d.baseIf}`;
    const statusLine = document.createElement("div");
    statusLine.style.color = color;
    statusLine.textContent = `Estado: ${st}`;
    const trafficLine = document.createElement("div");
    trafficLine.className = "text-slate-300";
    trafficLine.textContent = `IN: ${inVal} Kbps | OUT: ${outVal} Kbps`;
    tooltip.append(title, statusLine, trafficLine);
    tooltip.classList.remove("hidden");
    positionTooltip(event);
  });
  state.cy.on("mousemove", "edge", positionTooltip);
  state.cy.on("mouseout", "edge", () => tooltip?.classList.add("hidden"));

  if ("ResizeObserver" in window) {
    new ResizeObserver(() => state.cy && state.cy.resize()).observe(container);
  }
  window.addEventListener("resize", () => state.cy && state.cy.resize());
  if (document.fonts?.ready) {
    document.fonts.ready.then(() => state.cy && state.cy.style().update());
  }
}

function setLayoutPressed(kind) {
  document.querySelectorAll("[data-layout]").forEach((btn) => {
    btn.setAttribute("aria-pressed", String(btn.dataset.layout === kind));
  });
}

function runLayout(kind) {
  if (!state.cy) return;
  const animate = !REDUCED_MOTION;
  let options;

  if (kind === "radial") {
    options = {
      name: "concentric",
      concentric: (node) => ROLE_RANK[node.data("role")] ?? 1,
      levelWidth: () => 1,
      minNodeSpacing: 140,
      padding: 80,
      animate,
      animationDuration: 550,
    };
  } else {
    const roots = state.cy.nodes('[role = "core"]');
    options = {
      name: "breadthfirst",
      directed: true,
      roots: roots.nonempty() ? roots : undefined,
      spacingFactor: 1.75,
      padding: 80,
      animate,
      animationDuration: 550,
    };
  }

  state.cy.layout(options).run();
  setLayoutPressed(kind);
}

function centerMap() {
  if (!state.cy) return;
  state.cy.animate(
    { fit: { eles: state.cy.elements(), padding: 80 } },
    { duration: REDUCED_MOTION ? 0 : 420 },
  );
}

/** Animación de alerta: línea discontinua en movimiento y halo pulsante en elementos down. */
function startAlertAnimation() {
  if (REDUCED_MOTION) return;
  let dashOffset = 0;
  let lastFrame = 0;

  const frame = (ts) => {
    if (state.cy && ts - lastFrame >= 40) {
      lastFrame = ts;
      const downEdges = state.cy.edges('[status = "down"]');
      const downNodes = state.cy.nodes('[status = "down"]');
      if (downEdges.nonempty() || downNodes.nonempty()) {
        dashOffset = (dashOffset - 1.5) % 1000;
        const pulse = 0.16 + 0.24 * (0.5 + 0.5 * Math.sin(ts / 260));
        state.cy.batch(() => {
          downEdges.style({ "line-dash-offset": dashOffset, "underlay-opacity": pulse });
          downNodes.style("underlay-opacity", pulse + 0.05);
        });
      }
    }
    requestAnimationFrame(frame);
  };
  requestAnimationFrame(frame);
}

// ---------------------------------------------------------------------------
// Historial y gráficas (Chart.js)
// ---------------------------------------------------------------------------

function getHistory(nodeId) {
  if (!state.historyByNode.has(nodeId)) {
    state.historyByNode.set(nodeId, { labels: [], bwIn: [], bwOut: [], cpu: [] });
  }
  return state.historyByNode.get(nodeId);
}

function recordHistory(snapshot) {
  const label = formatUtc(snapshot.timestamp);
  for (const metric of snapshot.nodes) {
    const hist = getHistory(metric.id);
    hist.labels.push(label);
    hist.bwIn.push(metric.bw_in_kbps ?? 0);
    hist.bwOut.push(metric.bw_out_kbps ?? 0);
    hist.cpu.push(metric.cpu_percent ?? null);
    if (hist.labels.length > MAX_SAMPLES) {
      hist.labels.shift();
      hist.bwIn.shift();
      hist.bwOut.shift();
      hist.cpu.shift();
    }
  }
}

function verticalGradient(chart, stops) {
  const { ctx, chartArea } = chart;
  if (!chartArea) return "transparent";
  const gradient = ctx.createLinearGradient(0, chartArea.top, 0, chartArea.bottom);
  for (const [offset, color] of stops) gradient.addColorStop(offset, color);
  return gradient;
}

const cpuThresholdPlugin = {
  id: "cpuThreshold",
  afterDatasetsDraw(chart) {
    const yScale = chart.scales.y;
    const { ctx, chartArea } = chart;
    if (!yScale || !chartArea) return;
    const y = yScale.getPixelForValue(CPU_WARN_PERCENT);
    ctx.save();
    ctx.strokeStyle = "rgba(255, 183, 3, 0.55)";
    ctx.setLineDash([4, 4]);
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(chartArea.left, y);
    ctx.lineTo(chartArea.right, y);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = "rgba(255, 183, 3, 0.85)";
    ctx.font = `10px ${MONO_FONT}`;
    ctx.fillText(`${CPU_WARN_PERCENT}% UMBRAL`, chartArea.left + 4, y - 4);
    ctx.restore();
  },
};

function baseChartOptions() {
  return {
    responsive: true,
    maintainAspectRatio: false,
    animation: false,
    interaction: { mode: "index", intersect: false },
    plugins: {
      legend: {
        labels: { color: "#94a3b8", boxWidth: 8, boxHeight: 8, usePointStyle: true, font: { size: 10 } },
      },
      tooltip: {
        backgroundColor: "rgba(5, 8, 15, 0.95)",
        borderColor: "rgba(6, 182, 212, 0.4)",
        borderWidth: 1,
        titleColor: "#a5f3fc",
        bodyColor: "#e2e8f0",
        padding: 8,
      },
    },
    scales: {
      x: {
        ticks: { color: "#475569", maxTicksLimit: 5, font: { size: 9 } },
        grid: { color: "rgba(6, 182, 212, 0.06)" },
        border: { color: "rgba(6, 182, 212, 0.2)" },
      },
      y: {
        beginAtZero: true,
        ticks: { color: "#475569", font: { size: 9 } },
        grid: { color: "rgba(6, 182, 212, 0.06)" },
        border: { color: "rgba(6, 182, 212, 0.2)" },
      },
    },
  };
}

function destroyCharts() {
  state.bwChart?.destroy();
  state.cpuChart?.destroy();
  state.bwChart = null;
  state.cpuChart = null;
}

function createCharts(nodeId) {
  destroyCharts();
  if (typeof window.Chart !== "function") return;

  const bwCanvas = $("chart-bw");
  const cpuCanvas = $("chart-cpu");
  if (!bwCanvas || !cpuCanvas) return;
  const hist = getHistory(nodeId);

  state.bwChart = new window.Chart(bwCanvas, {
    type: "line",
    data: {
      labels: [...hist.labels],
      datasets: [
        {
          label: "IN (Kbps)",
          data: [...hist.bwIn],
          borderColor: ACCENTS.cyan,
          borderWidth: 2,
          pointRadius: 0,
          pointHoverRadius: 3,
          tension: 0.35,
          fill: true,
          backgroundColor: (ctx) =>
            verticalGradient(ctx.chart, [[0, "rgba(6, 182, 212, 0.38)"], [1, "rgba(6, 182, 212, 0)"]]),
        },
        {
          label: "OUT (Kbps)",
          data: [...hist.bwOut],
          borderColor: ACCENTS.magenta,
          borderWidth: 2,
          pointRadius: 0,
          pointHoverRadius: 3,
          tension: 0.35,
          fill: true,
          backgroundColor: (ctx) =>
            verticalGradient(ctx.chart, [[0, "rgba(217, 70, 239, 0.3)"], [1, "rgba(139, 92, 246, 0)"]]),
        },
      ],
    },
    options: baseChartOptions(),
  });

  const cpuOptions = baseChartOptions();
  cpuOptions.scales.y = { ...cpuOptions.scales.y, beginAtZero: true, min: 0, max: 100 };

  state.cpuChart = new window.Chart(cpuCanvas, {
    type: "line",
    data: {
      labels: [...hist.labels],
      datasets: [
        {
          label: "CPU (%)",
          data: [...hist.cpu],
          borderColor: STATUS_COLORS.up,
          borderWidth: 2,
          pointRadius: 0,
          pointHoverRadius: 3,
          tension: 0.35,
          spanGaps: false,
          fill: true,
          segment: {
            borderColor: (ctx) => ((ctx.p1.parsed.y ?? 0) >= CPU_WARN_PERCENT ? STATUS_COLORS.degraded : STATUS_COLORS.up),
          },
          backgroundColor: (ctx) =>
            verticalGradient(ctx.chart, [
              [0, "rgba(255, 183, 3, 0.4)"],
              [0.45, "rgba(0, 245, 155, 0.22)"],
              [1, "rgba(0, 245, 155, 0)"],
            ]),
        },
      ],
    },
    options: cpuOptions,
    plugins: [cpuThresholdPlugin],
  });
}

function refreshCharts(nodeId) {
  const hist = getHistory(nodeId);
  if (state.bwChart) {
    state.bwChart.data.labels = [...hist.labels];
    state.bwChart.data.datasets[0].data = [...hist.bwIn];
    state.bwChart.data.datasets[1].data = [...hist.bwOut];
    state.bwChart.update("none");
  }
  if (state.cpuChart) {
    state.cpuChart.data.labels = [...hist.labels];
    state.cpuChart.data.datasets[0].data = [...hist.cpu];
    state.cpuChart.update("none");
  }
}

// ---------------------------------------------------------------------------
// Inspector HUD (#node-panel)
// ---------------------------------------------------------------------------

function renderBar(el, lit, color, value, valueText) {
  if (!el) return;
  if (el.childElementCount !== BAR_SEGMENTS) {
    el.replaceChildren();
    for (let i = 0; i < BAR_SEGMENTS; i += 1) {
      const seg = document.createElement("span");
      seg.className = "seg";
      el.appendChild(seg);
    }
  }
  el.style.setProperty("--seg-color", color);
  Array.from(el.children).forEach((seg, i) => seg.classList.toggle("is-on", i < lit));
  el.setAttribute("aria-valuenow", String(value));
  el.setAttribute("aria-valuetext", valueText);
}

function updateInspector() {
  const nodeId = state.selectedNodeId;
  if (!nodeId) return;

  const meta = state.topology.nodes.find((n) => n.id === nodeId);
  const metric = state.latestSnapshot?.nodes?.find((n) => n.id === nodeId);
  const status = metric?.status ?? "up";
  const color = STATUS_COLORS[status] ?? STATUS_COLORS.down;

  $("panel-node-title").textContent = meta?.name ?? nodeId;
  $("panel-node-ip").textContent = meta?.ip ?? "--";
  $("panel-node-role").textContent = meta
    ? `${ROLE_LABELS[meta.role] ?? String(meta.role).toUpperCase()} · ${String(meta.type).toUpperCase()}`
    : "--";

  const chip = $("panel-node-status");
  chip.dataset.status = status;
  chip.textContent = `${STATUS_LABELS[status] ?? "N/D"} · ${status}`;

  if (meta) $("panel-node-icon").src = getNodeIcon(meta.role, meta.type, color);
  $("panel-icon-wrap").style.setProperty("--node-glow", color);

  // Latencia: escala 0–100 ms en 20 segmentos.
  const latency = metric?.latency_ms;
  const latencyEl = $("spec-latency");
  if (latency === null || latency === undefined) {
    renderBar($("bar-latency"), BAR_SEGMENTS, STATUS_COLORS.down, 0, "Sin respuesta");
    latencyEl.textContent = "SIN RESPUESTA";
    latencyEl.dataset.tone = "down";
  } else {
    const lit = Math.min(BAR_SEGMENTS, Math.max(1, Math.ceil((latency / LATENCY_SCALE_MS) * BAR_SEGMENTS)));
    const tone = latency >= LATENCY_WARN_MS ? "degraded" : "up";
    renderBar($("bar-latency"), lit, STATUS_COLORS[tone], latency, `${latency} ms`);
    latencyEl.textContent = formatNum(latency, " ms", 2);
    latencyEl.dataset.tone = tone;
  }

  // Pérdida de paquetes: 0–100 % en 20 segmentos.
  const loss = Number(metric?.packet_loss ?? 0);
  const lossTone = loss >= 100 ? "down" : loss > 0 ? "degraded" : "up";
  const lossLit = Math.min(BAR_SEGMENTS, Math.ceil((loss / 100) * BAR_SEGMENTS));
  renderBar($("bar-loss"), lossLit, STATUS_COLORS[lossTone], loss, `${loss} %`);
  const lossEl = $("spec-loss");
  lossEl.textContent = formatNum(loss, " %", 1);
  lossEl.dataset.tone = lossTone;

  $("spec-cpu").textContent = formatNum(metric?.cpu_percent, " %", 1);
  $("spec-bw-in").textContent = formatKbps(metric?.bw_in_kbps);
  $("spec-bw-out").textContent = formatKbps(metric?.bw_out_kbps);

  refreshCharts(nodeId);
}

function selectNode(nodeId) {
  const changed = state.selectedNodeId !== nodeId;
  state.selectedNodeId = nodeId;

  $("panel-empty").classList.add("hidden");
  const content = $("panel-content");
  content.classList.remove("hidden");
  content.classList.add("flex");
  $("close-panel-btn").classList.remove("hidden");

  if (state.cy) {
    const node = state.cy.getElementById(nodeId);
    state.cy.batch(() => {
      state.cy.elements().unselect().addClass("dimmed");
      node.closedNeighborhood().removeClass("dimmed");
      node.select();
    });
    state.cy.resize();
    setTimeout(() => state.cy && state.cy.resize(), 150);
  }

  if (changed || !state.bwChart || !state.cpuChart) createCharts(nodeId);
  updateInspector();
  updateQuickSelect();
}

function clearSelection() {
  state.selectedNodeId = null;
  destroyCharts();

  const content = $("panel-content");
  content.classList.add("hidden");
  content.classList.remove("flex");
  $("panel-empty").classList.remove("hidden");
  $("close-panel-btn").classList.add("hidden");

  if (state.cy) {
    state.cy.batch(() => state.cy.elements().removeClass("dimmed").unselect());
    state.cy.resize();
    setTimeout(() => state.cy && state.cy.resize(), 150);
  }
  updateQuickSelect();
}

function buildQuickSelect(topology) {
  const container = $("node-quick-select");
  if (!container) return;
  container.replaceChildren();
  state.quickChips.clear();

  for (const node of topology.nodes) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "node-chip";
    btn.dataset.status = "up";
    btn.setAttribute("aria-pressed", "false");
    const dot = document.createElement("span");
    dot.className = "node-chip-dot";
    dot.setAttribute("aria-hidden", "true");
    btn.append(dot, document.createTextNode(node.id));
    btn.addEventListener("click", () => selectNode(node.id));
    container.appendChild(btn);
    state.quickChips.set(node.id, btn);
  }
}

function updateQuickSelect() {
  const statusById = new Map((state.latestSnapshot?.nodes ?? []).map((n) => [n.id, n.status]));
  for (const [nodeId, btn] of state.quickChips) {
    btn.dataset.status = statusById.get(nodeId) ?? "up";
    btn.setAttribute("aria-pressed", String(state.selectedNodeId === nodeId));
  }
}

// ---------------------------------------------------------------------------
// Terminal de eventos (#events-log)
// ---------------------------------------------------------------------------

function pushLogLine({ key = null, time, sev, text }) {
  if (key) {
    if (state.logKeys.has(key)) return;
    state.logKeys.add(key);
  }

  const list = $("events-list");
  if (!list) return;
  $("events-empty")?.remove();

  const nearBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 32;

  const li = document.createElement("li");
  li.className = "log-line";
  li.dataset.sev = sev;

  const timeEl = document.createElement("span");
  timeEl.className = "log-time";
  timeEl.textContent = `[${time}]`;
  const tagEl = document.createElement("span");
  tagEl.className = "log-tag";
  tagEl.textContent = `[${sev}]`;
  const textEl = document.createElement("span");
  textEl.className = "log-text";
  textEl.textContent = text;

  li.append(timeEl, tagEl, textEl);
  list.appendChild(li);

  while (list.childElementCount > MAX_LOG_LINES) list.firstElementChild.remove();

  $("events-count").textContent = `${list.childElementCount} líneas`;
  if (nearBottom) list.scrollTop = list.scrollHeight;
}

function logServerEvent(evt) {
  if (!evt || evt.id === undefined || evt.id === null) return;
  pushLogLine({
    key: `evt-${evt.id}`,
    time: formatUtc(evt.timestamp),
    sev: SEVERITY_BY_KIND[evt.kind] ?? "SYS",
    text: `${evt.message} [${evt.previous} → ${evt.current}]`,
  });
}

function logSystem(text, sev = "SYS") {
  pushLogLine({ time: formatUtc(), sev, text });
}

// ---------------------------------------------------------------------------
// Dock de inyección de fallas (#sim-controls)
// ---------------------------------------------------------------------------

function buildSimRow(kind, id, subtitle) {
  const li = document.createElement("li");
  li.className = "sim-row";

  const info = document.createElement("div");
  info.className = "min-w-0";
  const title = document.createElement("p");
  title.className = "sim-row-title";
  title.textContent = id;
  const sub = document.createElement("p");
  sub.className = "sim-row-sub";
  sub.textContent = subtitle;
  const statusEl = document.createElement("p");
  statusEl.className = "sim-row-status";
  statusEl.dataset.status = "up";
  statusEl.textContent = STATUS_LABELS.up;
  info.append(title, sub, statusEl);

  const button = document.createElement("button");
  button.type = "button";
  button.className = "tac-switch";
  button.setAttribute("role", "switch");
  button.setAttribute("aria-checked", "true");
  button.setAttribute("aria-label", `${kind === "node" ? "Nodo" : "Enlace"} ${id} operativo`);
  const thumb = document.createElement("span");
  thumb.className = "tac-switch-thumb";
  thumb.setAttribute("aria-hidden", "true");
  button.appendChild(thumb);
  button.addEventListener("click", () => toggleFault(kind, id));

  li.append(info, button);
  state.simControls.set(`${kind}:${id}`, { button, statusEl, busy: false });
  return li;
}

function setupSimulationControls(topology) {
  const section = $("sim-controls");
  const dock = $("dock");
  if (!section || !dock) return;

  if (!state.simulation) {
    section.classList.add("hidden");
    section.classList.remove("flex");
    dock.classList.remove("lg:grid-cols-[420px_minmax(0,1fr)]");
    return;
  }

  section.classList.remove("hidden");
  section.classList.add("flex");

  const nodeList = $("sim-node-list");
  const edgeList = $("sim-edge-list");
  nodeList.replaceChildren();
  edgeList.replaceChildren();
  state.simControls.clear();

  for (const node of topology.nodes) {
    nodeList.appendChild(buildSimRow("node", node.id, `${node.ip} · ${ROLE_LABELS[node.role] ?? node.role}`));
  }
  for (const edge of topology.edges) {
    const path = `${edge.source}:${shortIf(edge.source_if?.name)} ⇄ ${edge.target}:${shortIf(edge.target_if?.name)}`;
    edgeList.appendChild(buildSimRow("edge", edge.id, path));
  }
}

/** Deduce desde el snapshot qué elementos tienen una falla inyectada. */
function syncForcedState(snapshot) {
  if (!state.simulation) return;
  for (const node of snapshot.nodes) {
    if (node.status === "down") state.forcedNodes.add(node.id);
    else state.forcedNodes.delete(node.id);
  }
  for (const edge of snapshot.edges) {
    // oper_status = 2 identifica un enlace tumbado manualmente en el simulador.
    if (edge.oper_status === 2) state.forcedEdges.add(edge.id);
    else if (edge.status !== "down") state.forcedEdges.delete(edge.id);
  }
}

function updateSimControls() {
  if (!state.simulation || !state.latestSnapshot) return;

  const apply = (kind, metric, forcedSet) => {
    const entry = state.simControls.get(`${kind}:${metric.id}`);
    if (!entry) return;
    const forced = forcedSet.has(metric.id);
    entry.button.setAttribute("aria-checked", String(!forced));
    entry.statusEl.dataset.status = metric.status;
    if (forced) entry.statusEl.textContent = `${STATUS_LABELS.down} · FALLA INYECTADA`;
    else if (metric.status === "down") entry.statusEl.textContent = `${STATUS_LABELS.down} · PROPAGADO`;
    else entry.statusEl.textContent = STATUS_LABELS[metric.status] ?? metric.status;
  };

  for (const node of state.latestSnapshot.nodes) apply("node", node, state.forcedNodes);
  for (const edge of state.latestSnapshot.edges) apply("edge", edge, state.forcedEdges);
}

async function toggleFault(kind, elementId) {
  const entry = state.simControls.get(`${kind}:${elementId}`);
  if (!entry || entry.busy) return;

  const feedback = $("sim-feedback");
  const endpoint =
    kind === "node"
      ? `/api/simulate/toggle-node/${encodeURIComponent(elementId)}`
      : `/api/simulate/toggle-link/${encodeURIComponent(elementId)}`;

  entry.busy = true;
  entry.button.disabled = true;
  entry.button.setAttribute("aria-busy", "true");
  feedback.textContent = `> Transmitiendo orden a ${elementId}…`;

  try {
    const response = await fetch(endpoint, { method: "POST" });
    let data = {};
    try {
      data = await response.json();
    } catch {
      data = {};
    }
    if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);

    const forcedSet = kind === "node" ? state.forcedNodes : state.forcedEdges;
    if (data.forced_down) forcedSet.add(elementId);
    else forcedSet.delete(elementId);

    const action = data.forced_down ? "inyectó falla en" : "restauró";
    feedback.textContent = `> ${elementId}: estado ${String(data.status).toUpperCase()}`;
    logSystem(`Operador ${action} ${elementId} → ${data.status}`);
  } catch (err) {
    feedback.textContent = `> Error en ${elementId}: ${err.message}`;
    logSystem(`Orden rechazada para ${elementId}: ${err.message}`, "CRIT");
  } finally {
    entry.busy = false;
    entry.button.disabled = false;
    entry.button.removeAttribute("aria-busy");
    updateSimControls();
  }
}

// ---------------------------------------------------------------------------
// Aplicación de snapshots de telemetría
// ---------------------------------------------------------------------------

function updateKpis(snapshot) {
  let up = 0;
  let degraded = 0;
  let down = 0;
  for (const node of snapshot.nodes) {
    if (node.status === "up") up += 1;
    else if (node.status === "degraded") degraded += 1;
    else down += 1;
  }
  $("kpi-up").textContent = String(up);
  $("kpi-degraded").textContent = String(degraded);
  $("kpi-down").textContent = String(down);
  $("kpi-down-pill").classList.toggle("is-alert", down > 0);
}

function applySnapshot(msg) {
  if (!msg || msg.type !== "telemetry" || !Array.isArray(msg.nodes) || !Array.isArray(msg.edges)) {
    return;
  }

  state.latestSnapshot = msg;
  recordHistory(msg);
  updateKpis(msg);
  $("last-sync").textContent = `CICLO ${formatUtc(msg.timestamp)}`;

  if (state.cy) {
    state.cy.batch(() => {
      for (const metric of msg.nodes) {
        const el = state.cy.getElementById(metric.id);
        if (el.empty()) continue;
        const color = STATUS_COLORS[metric.status] ?? STATUS_COLORS.down;
        el.data({
          status: metric.status,
          color,
          icon: getNodeIcon(el.data("role"), el.data("type"), color),
          label: nodeLabel(el.data("name"), el.data("ip"), metric),
        });
        if (metric.status !== "down") el.removeStyle("underlay-opacity");
      }

      for (const metric of msg.edges) {
        const el = state.cy.getElementById(metric.id);
        if (el.empty()) continue;
        el.data({
          status: metric.status,
          bwIn: metric.bw_in_kbps ?? 0,
          bwOut: metric.bw_out_kbps ?? 0,
          color: STATUS_COLORS[metric.status] ?? STATUS_COLORS.down,
        });
        if (metric.status !== "down") el.removeStyle("underlay-opacity line-dash-offset");
      }
    });
  }

  syncForcedState(msg);
  updateSimControls();
  updateQuickSelect();

  if (Array.isArray(msg.events)) {
    for (const evt of msg.events) logServerEvent(evt);
  }
  if (state.selectedNodeId) updateInspector();
}

// ---------------------------------------------------------------------------
// WebSocket con reconexión exponencial
// ---------------------------------------------------------------------------

function setWsStatus(kind, label) {
  const indicator = $("ws-status");
  if (indicator) indicator.dataset.state = kind;
  const labelEl = $("ws-label");
  if (labelEl) labelEl.textContent = label;
}

function connectWebSocket() {
  if (state.reconnectTimer) {
    clearTimeout(state.reconnectTimer);
    state.reconnectTimer = null;
  }

  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(`${protocol}//${window.location.host}/ws/telemetry`);
  state.ws = ws;
  setWsStatus("reconnecting", "RECONNECTING");

  ws.addEventListener("open", () => {
    state.reconnectDelayMs = BASE_RECONNECT_MS;
    state.wsConnected = true;
    setWsStatus("connected", "WS CONNECTED");
    logSystem("Canal /ws/telemetry establecido.", "OK");
  });

  ws.addEventListener("message", (event) => {
    try {
      applySnapshot(JSON.parse(event.data));
    } catch {
      // Trama malformada: se ignora sin interrumpir el flujo.
    }
  });

  ws.addEventListener("close", () => {
    const delay = state.reconnectDelayMs;
    if (state.wsConnected) {
      logSystem(`Canal perdido; reintentando en ${Math.round(delay / 1000)}s.`, "WARN");
    }
    state.wsConnected = false;
    setWsStatus("reconnecting", `RECONNECTING · ${Math.round(delay / 1000)}s`);
    state.reconnectDelayMs = Math.min(state.reconnectDelayMs * 2, MAX_RECONNECT_MS);
    state.reconnectTimer = setTimeout(connectWebSocket, delay);
  });

  ws.addEventListener("error", () => ws.close());
}

// ---------------------------------------------------------------------------
// Arranque
// ---------------------------------------------------------------------------

function tickClock() {
  const clock = $("utc-clock");
  if (!clock) return;
  const now = new Date();
  clock.textContent = `${now.toISOString().slice(11, 19)} UTC`;
  clock.dateTime = now.toISOString();
}

function setModeBadge() {
  const badge = $("mode-badge");
  if (!badge) return;
  badge.dataset.mode = state.simulation ? "sim" : "lab";
  badge.textContent = state.simulation ? "SIM MODE" : "LAB MODE";
}

async function bootstrapDashboard() {
  tickClock();
  setInterval(tickClock, 1000);

  if (typeof window.Chart === "function") {
    window.Chart.defaults.font.family = MONO_FONT;
    window.Chart.defaults.color = "#64748b";
  }

  $("close-panel-btn")?.addEventListener("click", clearSelection);
  $("center-map-btn")?.addEventListener("click", centerMap);
  document.querySelectorAll("[data-layout]").forEach((btn) => {
    btn.addEventListener("click", () => runLayout(btn.dataset.layout));
  });

  logSystem("Inicializando consola táctica NOC…");

  try {
    const [configRes, topoRes, eventsRes] = await Promise.all([
      fetch("/api/config"),
      fetch("/api/topology"),
      fetch("/api/events"),
    ]);

    if (configRes.ok) {
      const cfg = await configRes.json();
      state.simulation = Boolean(cfg.simulation);
      state.pollInterval = Number(cfg.poll_interval_seconds) || 3;
      setModeBadge();
    }

    if (topoRes.ok) {
      state.topology = await topoRes.json();
      initCytoscape(state.topology);
      buildQuickSelect(state.topology);
      setupSimulationControls(state.topology);
      $("map-meta").textContent =
        `NODES ${state.topology.nodes.length} · LINKS ${state.topology.edges.length} · CYCLE ${state.pollInterval}s`;
    }

    if (eventsRes.ok) {
      const events = await eventsRes.json();
      if (Array.isArray(events)) {
        // La API entrega los más recientes primero; la terminal los muestra en orden cronológico.
        [...events].reverse().forEach(logServerEvent);
      }
    }

    const latestRes = await fetch("/api/telemetry/latest");
    if (latestRes.ok) applySnapshot(await latestRes.json());
  } catch (err) {
    setWsStatus("reconnecting", "API OFFLINE");
    logSystem(`Error en carga inicial: ${err.message}`, "CRIT");
  }

  connectWebSocket();
  startAlertAnimation();
}

document.addEventListener("DOMContentLoaded", bootstrapDashboard);
