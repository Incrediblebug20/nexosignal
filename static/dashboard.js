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
let portfolioChart = null;

function initCharts() {
  applyChartDefaults();

  const confCanvas = document.getElementById("confidenceChart");
  const dirCanvas  = document.getElementById("directionChart");
  const appCanvas  = document.getElementById("approvalChart");
  const portCanvas = document.getElementById("portfolioChart");

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

  if (portCanvas) {
    portfolioChart = new Chart(portCanvas, {
      type: "doughnut",
      data: {
        labels: ["Cash"],
        datasets: [
          {
            data: [1],
            backgroundColor: ["rgba(147,164,183,0.45)"],
            borderColor: ["#344252"],
            borderWidth: 1,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        cutout: "68%",
        plugins: {
          legend: { position: "bottom", labels: { padding: 12, boxWidth: 11 } },
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
    if (el("cockpit-equity")) el("cockpit-equity").textContent = fmt(d.portfolio_value);
    if (el("cockpit-cash"))   el("cockpit-cash").textContent   = fmt(d.cash);
    if (el("cockpit-bp"))     el("cockpit-bp").textContent     = fmt(d.buying_power);

    if (el("m-upl") && d.total_unrealized_pl !== undefined) {
      const pl = d.total_unrealized_pl;
      el("m-upl").innerHTML = `<span class="${pl >= 0 ? "good" : "bad"}">${fmt(pl)}</span>`;
    }
    if (el("cockpit-upl") && d.total_unrealized_pl !== undefined) {
      const pl = d.total_unrealized_pl;
      el("cockpit-upl").textContent = fmt(pl);
      el("cockpit-upl").className = pl >= 0 ? "good" : "bad";
    }

    updatePortfolioChart(d);
  } catch (e) {
    // silent — broker may be offline
  }
}

function updatePortfolioChart(data) {
  if (!portfolioChart) return;
  const positions = Array.isArray(data.positions) ? data.positions : [];
  const labels = positions.map((p) => p.symbol);
  const values = positions.map((p) => Math.max(0, Number(p.market_value || 0)));
  const cash = Math.max(0, Number(data.cash || 0));
  labels.push("Cash");
  values.push(cash);

  const palette = [
    "rgba(0,201,141,0.85)",
    "rgba(138,180,255,0.82)",
    "rgba(245,159,0,0.82)",
    "rgba(173,127,255,0.82)",
    "rgba(49,196,255,0.78)",
    "rgba(147,164,183,0.45)",
  ];

  portfolioChart.data.labels = labels;
  portfolioChart.data.datasets[0].data = values;
  portfolioChart.data.datasets[0].backgroundColor = labels.map((_, i) => palette[i % palette.length]);
  portfolioChart.update("none");
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
    renderMasterCard(d.local_master, d.local_llm_enabled, d.ollama_model);
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
  // Master card reset
  setText("master-signal-badge", "—");
  setClass("master-signal-badge", "signal-badge");
  setText("master-conf", "—");
  setText("master-sentiment", "—");
  setText("master-target", "—");
  setText("master-sl", "—");
  setText("master-rr", "—");
  setText("master-rationale", "Awaiting master synthesis…");

  setText("consensus-signal", "—");
  setText("consensus-conf", "Confidence: —");
  setText("rr-actual", "R:R —");
  setStyle("rr-meter-fill", "width", "0%");
  setText("verdict-icon", "⏳");
  setText("verdict-text", "Awaiting Research");
  setText("verdict-sub", "Run analysis to see the Master LLM trade decision");
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

  const masterActive = d.local_llm_enabled && d.local_master && !d.local_master.error;
  const approverLabel = masterActive ? "Local Master" : "Claude";

  if (d.approved_5to1 && d.master_decision && d.master_decision.approved) {
    setText("verdict-icon", "✅");
    setText("verdict-text", "TRADE APPROVED");
    setText("verdict-sub", `${approverLabel} confirmed ${(rr || 0).toFixed(2)}:1 ratio ≥ ${d.min_ratio_required || 5}:1 minimum`);
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
    setText("verdict-sub", `${approverLabel} did not approve${rrStr}`);
    setClass("verdict-box", "verdict-box rejected");
    document.getElementById("trade-levels")?.classList.add("hidden");
  }
}

function renderMasterCard(data, localLlmEnabled, ollamaModel) {
  const card = document.getElementById("card-master");
  if (!card) return;

  if (!localLlmEnabled) {
    // Dim the card and show disabled state
    card.classList.add("master-disabled");
    setText("master-rationale", `Local Master LLM is disabled. Set LOCAL_LLM_ENABLED=true in .env and run Ollama (${ollamaModel || "mistral"}) locally to activate.`);
    setText("master-signal-badge", "OFF");
    return;
  }

  card.classList.remove("master-disabled");

  if (!data) {
    setText("master-rationale", "⚠ No data returned from master.");
    return;
  }

  const badge = document.getElementById("master-signal-badge");
  if (badge) {
    badge.textContent = data.signal ? data.signal.toUpperCase() : "—";
    badge.className = `signal-badge ${data.signal || ""}`;
  }

  setText("master-conf",      data.confidence != null ? `${data.confidence.toFixed(0)}/100` : (data.error ? "Error" : "—"));
  // sentiment field stores agent_agreement level for the master
  const agreementLabels = { full: "Full", partial: "Partial", none: "None" };
  setText("master-sentiment", agreementLabels[data.sentiment] || data.sentiment || "—");
  setText("master-target",    data.price_target != null ? `$${data.price_target.toFixed(2)}` : "—");
  setText("master-sl",        data.stop_loss    != null ? `$${data.stop_loss.toFixed(2)}`    : "—");
  setText("master-rr",        data.risk_reward_ratio != null ? `${data.risk_reward_ratio.toFixed(2)}:1` : "—");
  setText("master-rationale", data.error ? `⚠ ${data.error}` : (data.rationale || "No master verdict provided"));
}

function showAIError(msg) {
  ["gemini", "grok", "claude"].forEach((p) => {
    setText(`${p}-rationale`, `⚠ ${msg}`);
  });
  setText("master-rationale", `⚠ ${msg}`);
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

  // SocketIO realtime listeners
  initSocketIO();

  // Auto-scroll bot log to bottom
  const log = document.getElementById("bot-log");
  if (log) log.scrollTop = log.scrollHeight;
});

// ── Intelligence Panel (Phase 2) ───────────────────────────────────────────

async function refreshIntelligence() {
  try {
    const res = await fetch("/api/intelligence");
    if (!res.ok) return;
    const data = await res.json();

    // Macro
    if (data.macro) {
      const m = data.macro;
      const regime = (m.regime || "neutral").replace("_", " ").toUpperCase();
      const pill = document.getElementById("regime-pill");
      if (pill) {
        pill.textContent = regime;
        pill.className = "pill " + (
          m.regime === "risk_on" ? "chip-good" :
          ["risk_off", "stagflation"].includes(m.regime) ? "chip-bad" : "chip-muted"
        );
      }
      _setText("macro-dgs10", m.dgs10 != null ? m.dgs10.toFixed(2) + "%" : "—");
      _setText("macro-dgs2",  m.dgs2  != null ? m.dgs2.toFixed(2)  + "%" : "—");
      _setText("macro-spread", m.spread != null ? (m.spread >= 0 ? "+" : "") + m.spread.toFixed(3) + "%" : "—");
      _setText("macro-claims", m.jobless_claims != null ? m.jobless_claims.toLocaleString() : "—");
    }

    // Watchlist leaderboard
    if (Array.isArray(data.watchlist) && data.watchlist.length > 0) {
      const tbody = document.getElementById("watchlist-tbody");
      if (tbody) {
        tbody.innerHTML = data.watchlist.slice(0, 15).map(w => `
          <tr>
            <td class="muted">${w.rank_position}</td>
            <td><strong>${w.symbol}</strong></td>
            <td><span class="pill chip-muted" style="font-size:10px">${w.category || "—"}</span></td>
            <td>
              <div class="score-bar">
                <div class="score-fill" style="width:${Math.round(w.composite_score || 0)}%"></div>
                <span>${Math.round(w.composite_score || 0)}</span>
              </div>
            </td>
            <td class="muted">${Math.round(w.score_growth || 0)}</td>
            <td class="muted">${Math.round(w.score_value  || 0)}</td>
            <td class="muted">${Math.round(w.score_sentiment || 0)}</td>
          </tr>
        `).join("");
      }
    }

    // Insider feed
    if (Array.isArray(data.insiders)) {
      const feed = document.getElementById("insider-feed");
      if (feed) {
        if (data.insiders.length === 0) {
          feed.innerHTML = '<div class="empty">No insider filings yet — Lens parses SEC EDGAR weekday 6 PM ET</div>';
        } else {
          feed.innerHTML = data.insiders.map(ins => {
            const cls = ins.transaction_type === "purchase" ? "insider-buy" :
                        ins.transaction_type === "sale" ? "insider-sell" : "";
            const pillCls = ins.transaction_type === "purchase" ? "chip-good" :
                            ins.transaction_type === "sale" ? "chip-bad" : "chip-muted";
            const shares = ins.shares ? ` ${ins.shares.toLocaleString()} shares` : "";
            const date = ins.filed_at ? ins.filed_at.slice(0, 10) : "";
            return `<div class="insider-row ${cls}">
              <div class="insider-symbol">${ins.symbol}</div>
              <div class="insider-details">
                <strong>${ins.insider_name || "Unknown"}</strong>
                <span class="muted">${ins.title || ""}</span>
              </div>
              <div class="insider-tx">
                <span class="pill ${pillCls}" style="font-size:10px">${ins.transaction_type || "?"}</span>
                <span class="muted" style="font-size:11px">${shares}</span>
              </div>
              <div class="muted" style="font-size:11px">${date}</div>
            </div>`;
          }).join("");
        }
      }
    }
  } catch (err) {
    console.warn("Intelligence panel refresh failed:", err);
  }
}

function _setText(id, value) {
  const el = document.getElementById(id);
  if (el) el.textContent = value;
}

// ── SocketIO Phase 2 realtime listeners ─────────────────────────────────────

function initSocketIO() {
  if (typeof io === "undefined") return;

  const socket = io({ transports: ["websocket", "polling"] });

  socket.on("connect", () => console.log("[NexoSignal] Realtime socket connected"));
  socket.on("disconnect", () => console.log("[NexoSignal] Realtime socket disconnected"));

  socket.on("watchlist_rebuilt", (data) => {
    refreshIntelligence();
    showToast(`Scout rebuilt watchlist — ${data.count ?? "?"} symbols`);
  });

  socket.on("macro_refreshed", (data) => {
    refreshIntelligence();
    const regime = (data.regime || "neutral").replace(/_/g, " ");
    showToast(`Macro regime updated: ${regime}`);
  });

  socket.on("insider_parsed", (data) => {
    if ((data.total_saved || 0) > 0) {
      refreshIntelligence();
      showToast(`Lens parsed ${data.total_saved} insider filing${data.total_saved === 1 ? "" : "s"}`);
    }
  });

  socket.on("circuit_breaker_update", (data) => {
    const msg = data.reason ? `Circuit breaker: ${data.reason}` : "Circuit breaker tripped";
    showToast(msg, data.active ? "error" : "info");
    refreshMetrics();
  });

  socket.on("position_update", () => {
    refreshMetrics();
  });

  // ── Order fill notification ─────────────────────────────────────────
  socket.on("order_fill", (data) => {
    const sym    = data.symbol || "";
    const status = (data.status || "").replace(/_/g, " ");
    const qty    = data.filled_qty    ? ` ${data.filled_qty} shares` : "";
    const price  = data.filled_avg_price
      ? ` @ $${parseFloat(data.filled_avg_price).toFixed(2)}`
      : "";
    const type = status === "filled" ? "success" : "info";
    showToast(`Order ${status}: ${sym}${qty}${price}`, type);
    if (status === "filled") refreshMetrics();
    // Update status badge on Trade page if visible
    const badge = document.querySelector(
      `[data-order-id="${data.order_id}"] .order-status-badge`
    );
    if (badge) {
      badge.textContent = status;
      badge.className = `order-status-badge ${status === "filled" ? "chip-good" : "chip-muted"}`;
    }
  });
}

function showToast(message, type = "info") {
  const el = document.createElement("div");
  el.className = `toast toast--${type}`;
  el.textContent = message;
  document.body.appendChild(el);
  requestAnimationFrame(() => el.classList.add("toast--visible"));
  setTimeout(() => {
    el.classList.remove("toast--visible");
    setTimeout(() => el.remove(), 350);
  }, 3700);
}

// Expose for inline onclick handlers
window.runAIResearch       = runAIResearch;
window.fetchRCPrice        = fetchRCPrice;
window.calcRiskReward      = calcRiskReward;
window.refreshIntelligence = refreshIntelligence;
