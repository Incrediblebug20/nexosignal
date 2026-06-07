/**
 * Dashboard JS: Chart.js analytics + AI Research panel + live metrics polling.
 */

"use strict";

// ── Chart.js global defaults (dark theme) ─────────────────────────────────
function applyChartDefaults() {
  if (!window.Chart) return;
  Chart.defaults.color = "#6b7fa8";
  Chart.defaults.borderColor = "#2a3454";
  Chart.defaults.font.family = "Inter, ui-sans-serif, system-ui";
  Chart.defaults.font.size = 11;
  Chart.defaults.plugins.legend.labels.color = "#c8d4ef";
  Chart.defaults.plugins.tooltip.backgroundColor = "#1a2035";
  Chart.defaults.plugins.tooltip.borderColor = "#2a3454";
  Chart.defaults.plugins.tooltip.borderWidth = 1;
  Chart.defaults.plugins.tooltip.titleColor = "#f0f4ff";
  Chart.defaults.plugins.tooltip.bodyColor = "#c8d4ef";
  Chart.defaults.plugins.tooltip.padding = 10;
}

// ── Chart instances ────────────────────────────────────────────────────────
let confidenceChart = null;
let directionChart = null;
let approvalChart = null;

function initCharts() {
  applyChartDefaults();

  const confCanvas = document.getElementById("confidenceChart");
  const dirCanvas  = document.getElementById("directionChart");
  const appCanvas  = document.getElementById("approvalChart");

  if (confCanvas) {
    confidenceChart = new Chart(confCanvas, {
      type: "bar",
      data: {
        labels: [],
        datasets: [
          {
            label: "Buy",
            data: [],
            backgroundColor: "rgba(16,185,129,0.7)",
            borderColor: "#10b981",
            borderWidth: 1,
            borderRadius: 3,
          },
          {
            label: "Sell",
            data: [],
            backgroundColor: "rgba(239,68,68,0.7)",
            borderColor: "#ef4444",
            borderWidth: 1,
            borderRadius: 3,
          },
          {
            label: "Hold",
            data: [],
            backgroundColor: "rgba(107,127,168,0.5)",
            borderColor: "#6b7fa8",
            borderWidth: 1,
            borderRadius: 3,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        scales: {
          x: { grid: { color: "#1e2640" }, ticks: { maxRotation: 30, font: { size: 10 } } },
          y: { grid: { color: "#1e2640" }, beginAtZero: true, ticks: { precision: 0 } },
        },
        plugins: {
          legend: { display: true, position: "bottom" },
          tooltip: {
            callbacks: {
              label: (ctx) => ` ${ctx.dataset.label}: conf ${ctx.label} → ${ctx.raw} signal(s)`,
            },
          },
        },
      },
    });
  }

  if (dirCanvas) {
    directionChart = new Chart(dirCanvas, {
      type: "doughnut",
      data: {
        labels: ["Buy", "Sell", "Hold"],
        datasets: [
          {
            data: [0, 0, 0],
            backgroundColor: ["rgba(16,185,129,0.8)", "rgba(239,68,68,0.8)", "rgba(107,127,168,0.6)"],
            borderColor: ["#10b981", "#ef4444", "#6b7fa8"],
            borderWidth: 1,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        cutout: "65%",
        plugins: {
          legend: { position: "bottom", labels: { padding: 14, boxWidth: 12 } },
        },
      },
    });
  }

  if (appCanvas) {
    approvalChart = new Chart(appCanvas, {
      type: "doughnut",
      data: {
        labels: ["Approved", "Rejected"],
        datasets: [
          {
            data: [0, 0],
            backgroundColor: ["rgba(59,130,246,0.8)", "rgba(35,44,69,0.8)"],
            borderColor: ["#3b82f6", "#303d60"],
            borderWidth: 1,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        cutout: "65%",
        plugins: {
          legend: { position: "bottom", labels: { padding: 14, boxWidth: 12 } },
        },
      },
    });
  }

  // Initial load
  refreshCharts();
  // Auto-refresh every 30s
  setInterval(refreshCharts, 30_000);
}

async function refreshCharts() {
  try {
    const res = await fetch("/api/chart-data");
    if (!res.ok) return;
    const d = await res.json();

    // Confidence trend chart (buy/sell/hold per recent signal)
    if (confidenceChart && d.signal_trend && d.signal_trend.length) {
      const labels = d.signal_trend.map((s) => s.label.split(" ")[0]);
      const buyData  = d.signal_trend.map((s) => s.signal === "buy"  ? s.confidence : 0);
      const sellData = d.signal_trend.map((s) => s.signal === "sell" ? s.confidence : 0);
      const holdData = d.signal_trend.map((s) => s.signal === "hold" ? s.confidence : 0);
      confidenceChart.data.labels = labels;
      confidenceChart.data.datasets[0].data = buyData;
      confidenceChart.data.datasets[1].data = sellData;
      confidenceChart.data.datasets[2].data = holdData;
      confidenceChart.update("none");
    }

    // Direction donut
    if (directionChart && d.signal_direction) {
      const dir = d.signal_direction;
      directionChart.data.datasets[0].data = [dir.buy || 0, dir.sell || 0, dir.hold || 0];
      directionChart.update("none");
    }

    // Approval donut
    if (approvalChart && d.approval_stats) {
      const ap = d.approval_stats;
      approvalChart.data.datasets[0].data = [ap.approved || 0, ap.rejected || 0];
      approvalChart.update("none");
    }

    updateRefreshTime();
  } catch (e) {
    console.warn("Chart refresh failed:", e);
  }
}

function updateRefreshTime() {
  const el = document.getElementById("last-refresh");
  if (el) {
    const now = new Date();
    el.textContent = `Updated ${now.toLocaleTimeString()}`;
  }
}

// ── Live metrics polling ───────────────────────────────────────────────────
async function refreshMetrics() {
  try {
    const res = await fetch("/api/live-metrics");
    if (!res.ok) return;
    const d = await res.json();
    if (d.error) return;

    const fmt = (n) => "$" + n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    const el = (id) => document.getElementById(id);

    if (el("m-portfolio")) el("m-portfolio").textContent = fmt(d.portfolio_value);
    if (el("m-cash"))      el("m-cash").textContent      = fmt(d.cash);
    if (el("m-bp"))        el("m-bp").textContent        = fmt(d.buying_power);

    if (el("m-upl") && d.total_unrealized_pl !== undefined) {
      const pl = d.total_unrealized_pl;
      el("m-upl").innerHTML = `<span class="${pl >= 0 ? "good" : "bad"}">${fmt(pl)}</span>`;
    }
  } catch (e) {
    // silent — broker may be offline
  }
}

// ── AI Research Panel ──────────────────────────────────────────────────────
async function runAIResearch() {
  const symbolInput = document.getElementById("ai-symbol-input");
  const symbol = symbolInput ? symbolInput.value.trim().toUpperCase() : "";
  if (!symbol) { symbolInput && symbolInput.focus(); return; }

  const btn = document.getElementById("ai-run-btn");
  const loading = document.getElementById("ai-loading");

  if (btn) { btn.disabled = true; btn.textContent = "Analyzing…"; }
  if (loading) loading.classList.remove("hidden");
  clearAIPanel();

  try {
    const res = await fetch(`/api/ai-research/${encodeURIComponent(symbol)}`);
    const d = await res.json();

    if (!res.ok || d.error) {
      showAIError(d.error || "Research failed");
      return;
    }

    renderAgentCard("gemini", d.gemini, d.current_price);
    renderAgentCard("grok",   d.grok,   d.current_price);
    renderAgentCard("claude", d.claude,  d.current_price);
    renderConsensus(d);

  } catch (e) {
    showAIError("Network error: " + e.message);
  } finally {
    if (btn)  { btn.disabled = false; btn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/></svg> Analyze'; }
    if (loading) loading.classList.add("hidden");
  }
}

function clearAIPanel() {
  ["gemini", "grok", "claude"].forEach((p) => {
    setText(`${p}-signal-badge`, "—");
    setClass(`${p}-signal-badge`, "signal-badge");
    setText(`${p}-conf`, "—");
    setText(`${p}-sentiment`, "—");
    setText(`${p}-target`, "—");
    setText(`${p}-sl`, "—");
    setText(`${p}-rr`, "—");
    setText(`${p}-rationale`, "Awaiting analysis…");
    if (p === "grok") setText("grok-catalyst", "—");
  });
  setText("consensus-signal", "—");
  setText("consensus-conf", "Confidence: —");
  setText("rr-actual", "R:R —");
  setStyle("rr-meter-fill", "width", "0%");
  setText("verdict-icon", "⏳");
  setText("verdict-text", "Awaiting Research");
  setText("verdict-sub", "Run analysis to see Claude's trade decision");
  setClass("verdict-box", "verdict-box");
  document.getElementById("trade-levels")?.classList.add("hidden");
}

function renderAgentCard(provider, data, currentPrice) {
  if (!data) return;

  const badge = document.getElementById(`${provider}-signal-badge`);
  if (badge) {
    badge.textContent = data.signal ? data.signal.toUpperCase() : "—";
    badge.className = `signal-badge ${data.signal || ""}`;
  }

  setText(`${provider}-conf`, data.confidence != null ? `${data.confidence.toFixed(0)}/100` : (data.error ? "Error" : "—"));
  setText(`${provider}-sentiment`, data.sentiment || "—");
  setText(`${provider}-target`, data.price_target != null ? `$${data.price_target.toFixed(2)}` : "—");
  setText(`${provider}-sl`, data.stop_loss != null ? `$${data.stop_loss.toFixed(2)}` : "—");
  if (document.getElementById(`${provider}-rr`)) {
    setText(`${provider}-rr`, data.risk_reward_ratio != null ? `${data.risk_reward_ratio.toFixed(2)}:1` : "—");
  }
  if (provider === "grok" && document.getElementById("grok-catalyst")) {
    setText("grok-catalyst", data.news_catalyst || "None");
  }
  setText(`${provider}-rationale`, data.error ? `⚠ ${data.error}` : (data.rationale || "No rationale provided"));
}

function renderConsensus(d) {
  const signal = (d.consensus_signal || "hold").toUpperCase();
  const conf   = d.consensus_confidence || 0;
  const rr     = d.risk_reward_ratio;
  const maxRR  = 10;
  const pct    = rr != null ? Math.min(rr / maxRR * 100, 100) : 0;

  setText("consensus-signal", signal);
  setClass("consensus-signal", `consensus-signal ${signal === "BUY" ? "good" : signal === "SELL" ? "bad" : ""}`);
  setText("consensus-conf", `Confidence: ${conf.toFixed(1)}%`);
  setStyle("rr-meter-fill", "width", `${pct}%`);
  setText("rr-actual", rr != null ? `R:R ${rr.toFixed(2)}:1 ${rr >= (d.min_ratio_required || 5) ? "✓" : "✗"}` : "R:R —");

  if (d.approved_5to1) {
    setText("verdict-icon", "✅");
    setText("verdict-text", "TRADE APPROVED");
    setText("verdict-sub", `Claude confirmed ${(rr || 0).toFixed(2)}:1 ratio ≥ ${d.min_ratio_required || 5}:1 minimum`);
    setClass("verdict-box", "verdict-box approved");

    // Show trade levels
    const levels = document.getElementById("trade-levels");
    if (levels) {
      levels.classList.remove("hidden");
      setText("tl-entry",  d.entry_price  != null ? `$${d.entry_price.toFixed(2)}`  : "—");
      setText("tl-sl",     d.stop_loss    != null ? `$${d.stop_loss.toFixed(2)}`    : "—");
      setText("tl-tp",     d.take_profit  != null ? `$${d.take_profit.toFixed(2)}`  : "—");
      setText("tl-rr",     rr != null ? `${rr.toFixed(2)}:1` : "—");
    }
  } else {
    const isHold = d.consensus_signal === "hold";
    setText("verdict-icon", isHold ? "⏸" : "❌");
    setText("verdict-text", isHold ? "NO CLEAR SIGNAL" : "TRADE REJECTED");
    const rrStr = rr != null ? ` (R:R ${rr.toFixed(2)}:1 < ${d.min_ratio_required || 5}:1 required)` : "";
    setText("verdict-sub", `Claude did not approve${rrStr}`);
    setClass("verdict-box", "verdict-box rejected");
    document.getElementById("trade-levels")?.classList.add("hidden");
  }
}

function showAIError(msg) {
  ["gemini", "grok", "claude"].forEach((p) => {
    setText(`${p}-rationale`, `⚠ ${msg}`);
  });
}

// ── 5:1 Risk Calculator ────────────────────────────────────────────────────
async function fetchRCPrice() {
  const sym = (document.getElementById("rc-symbol")?.value || "").trim().toUpperCase();
  if (!sym) return;
  try {
    const res = await fetch(`/api/price/${encodeURIComponent(sym)}`);
    const d = await res.json();
    if (d.error) { alert(d.error); return; }
    const entryEl = document.getElementById("rc-entry");
    const riskEl  = document.getElementById("rc-risk");
    if (entryEl) entryEl.value = d.price.toFixed(2);
    if (riskEl && d.atr)  riskEl.value = d.atr.toFixed(2);
  } catch (e) {
    alert("Could not fetch price: " + e.message);
  }
}

function calcRiskReward() {
  const entry = parseFloat(document.getElementById("rc-entry")?.value || "0");
  const risk  = parseFloat(document.getElementById("rc-risk")?.value  || "0");
  if (!entry || !risk) return;

  const sl  = entry - risk;
  const tp  = entry + risk * 5;
  const results = document.getElementById("rc-results");
  if (results) {
    results.classList.remove("hidden");
    setText("rc-sl",      `$${sl.toFixed(2)}`);
    setText("rc-tp",      `$${tp.toFixed(2)}`);
    setText("rc-maxloss", `-$${risk.toFixed(2)}`);
    setText("rc-gain",    `+$${(risk * 5).toFixed(2)}`);
    setText("rc-rr",      "5.0 : 1 ✓");
  }
}

// ── Utility ────────────────────────────────────────────────────────────────
function setText(id, text) {
  const el = document.getElementById(id);
  if (el) el.textContent = text;
}

function setClass(id, cls) {
  const el = document.getElementById(id);
  if (el) el.className = cls;
}

function setStyle(id, prop, val) {
  const el = document.getElementById(id);
  if (el) el.style[prop] = val;
}

// Allow Enter key on symbol input
function bindAISymbolInput() {
  const inp = document.getElementById("ai-symbol-input");
  if (inp) {
    inp.addEventListener("keydown", (e) => {
      if (e.key === "Enter") runAIResearch();
    });
  }
}

// ── Init ───────────────────────────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", () => {
  if (window.Chart) {
    initCharts();
  } else {
    // Chart.js not loaded yet (CDN still fetching) — retry
    setTimeout(initCharts, 1000);
  }

  // Live metrics refresh every 60s
  refreshMetrics();
  setInterval(refreshMetrics, 60_000);

  bindAISymbolInput();

  // Auto-scroll bot log to bottom
  const log = document.getElementById("bot-log");
  if (log) log.scrollTop = log.scrollHeight;
});

// Expose for inline onclick handlers
window.runAIResearch  = runAIResearch;
window.fetchRCPrice   = fetchRCPrice;
window.calcRiskReward = calcRiskReward;
