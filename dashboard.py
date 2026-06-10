import logging
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
import requests
from dataclasses import asdict
from datetime import datetime, timezone
from functools import wraps
from typing import Callable

from flask import Flask, flash, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from trading_agent import config
from trading_agent.agent import NexoSignalAgent, register_agent_event_listener
from trading_agent.broker import AlpacaBroker, BrokerError
from trading_agent.restrictions import RejectedOrder, break_even_stop_update, get_tracker
from trading_agent.runtime import AgentRuntime
from trading_agent.signal_engine import (
    analyze_risk_reward,
    build_alpha_picks,
    build_ranked_predictions,
    compute_indicators,
    evaluate_strategy_suite,
)
from trading_agent.storage import (
    init_db,
    latest_wallet_connection,
    list_brokerage_connections,
    list_signal_events,
    list_trade_events,
    record_brokerage_connection,
    record_signal_event,
    record_trade_event,
    record_wallet_connection,
    signal_summary,
    trade_summary,
    update_trade_event_status,
    list_watchlist,
    latest_macro_snapshot,
    list_insider_activity,
    list_risk_metrics,
    list_performance_events,
    create_strategy_portfolio,
    list_strategy_portfolios,
    get_strategy_portfolio,
    update_strategy_portfolio,
    delete_strategy_portfolio,
    record_strategy_trade,
    cache_lens_report,
    get_cached_lens_report,
    list_lens_reports,
    record_backtest_run,
    list_backtest_runs,
)
from trading_agent.strategy import STRATEGIES, is_supported_crypto, normalize_symbol

logger = logging.getLogger("dashboard")

try:
    from flask_socketio import SocketIO
except Exception:
    SocketIO = None

app = Flask(__name__)
app.secret_key = config.FLASK_SECRET_KEY
socketio = SocketIO(app, cors_allowed_origins="*") if SocketIO else None
init_db()

_runtime: AgentRuntime | None = None
_agent_lock = threading.Lock()

# ── Runtime trading mode (can be toggled live without restart) ────────────────
_trading_mode: str = getattr(config, "TRADING_MODE", "paper")


def get_trading_mode() -> str:
    return _trading_mode


def set_trading_mode(mode: str) -> None:
    global _trading_mode
    _trading_mode = mode
    config.TRADING_MODE = mode  # type: ignore[assignment]


def _log_startup_config() -> None:
    checks = [
        ("Alpaca",               bool(config.ALPACA_API_KEY and config.ALPACA_SECRET_KEY)),
        ("Supabase",             bool(config.SUPABASE_DB_URL)),
        ("Telegram",             bool(config.TELEGRAM_TOKEN and config.TELEGRAM_CHAT_ID)),
        ("AI Research",          config.AI_RESEARCH_ENABLED),
        ("  └ Gemini",           bool(config.GEMINI_API_KEY)),
        ("  └ Grok",             bool(config.GROK_API_KEY)),
        ("  └ Claude",           bool(config.ANTHROPIC_API_KEY)),
        ("  └ Local LLM Master", config.LOCAL_LLM_ENABLED),
        ("Scout (FMP)",          bool(config.FMP_API_KEY)),
        ("Macro (FRED)",         bool(config.FRED_API_KEY)),
        ("Scheduler",            not config.RUNNING_ON_VERCEL),
    ]
    lines = ["", "=" * 46, "  NexoSignal Startup Configuration", "=" * 46]
    for name, ok in checks:
        lines.append(f"  {'✓' if ok else '✗'}  {name}")
    lines.append("=" * 46)
    logger.info("\n".join(lines))


_log_startup_config()


def publish_realtime_event(event_type: str, payload: dict) -> None:
    if socketio:
        socketio.emit(event_type, payload)


register_agent_event_listener(publish_realtime_event)


@app.context_processor
def inject_app_config():
    return {"config": config}


def authenticate_user(username: str, raw_password: str) -> bool:
    users = config.DASHBOARD_USERS
    if username not in users:
        return False
    expected = users[username]
    if expected.startswith(("pbkdf2:", "scrypt:")):
        return check_password_hash(expected, raw_password)
    return raw_password == expected


def login_required(view: Callable):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def get_broker() -> AlpacaBroker | None:
    try:
        return AlpacaBroker()
    except BrokerError:
        return None


def validate_alpaca_connection() -> str | None:
    try:
        AlpacaBroker().get_account()
        return None
    except BrokerError as e:
        return str(e)


def friendly_broker_error(error: str) -> str:
    if "401" in error or "unauthorized" in error.lower():
        return (
            "Alpaca rejected the API keys. Replace ALPACA_API_KEY and "
            "ALPACA_SECRET_KEY in .env with valid paper keys, then restart the dashboard."
        )
    return error


def current_agent_status() -> dict:
    with _agent_lock:
        runtime = _runtime
    if runtime:
        return runtime.status()
    return {
        "running": False, "configured": False, "scheduler_running": False,
        "scheduler_jobs": [], "symbols": [], "strategy": None,
        "qty": None, "interval": None, "dry_run": None, "log": [], "errors": [],
    }


_nexosignal_cache: dict[str, object] = {"ts": 0.0, "symbol_bars": {}, "source": "cold"}
_NEXOSIGNAL_TTL = 120


def _fallback_bars(symbol: str, price: float | None = None, count: int = 80) -> list[dict]:
    """Deterministic fallback bars for offline dashboard rendering."""
    base = float(price or 100.0)
    bars: list[dict] = []
    for idx in range(count):
        drift = (idx - count / 2) * 0.015
        wave = ((idx % 9) - 4) * 0.12
        close = max(0.5, base + drift + wave)
        open_ = close - 0.08
        high = close + 0.35
        low = max(0.01, close - 0.35)
        bars.append({"o": open_, "h": high, "l": low, "c": close, "v": 2_000_000 + idx * 3_000})
    return bars


def _load_symbol_bars(symbols: list[str] | None = None, limit: int = 80) -> tuple[dict[str, list[dict]], str, bool, str | None]:
    now = time.time()
    if _nexosignal_cache["symbol_bars"] and now - float(_nexosignal_cache["ts"]) < _NEXOSIGNAL_TTL:
        return dict(_nexosignal_cache["symbol_bars"]), str(_nexosignal_cache["source"]), False, None

    selected = symbols or config.DEFAULT_DASHBOARD_SYMBOLS or ["AAPL", "MSFT", "SPY", "QQQ", "BTC/USD", "ETH/USD"]
    selected = [normalize_symbol(s) for s in selected[:20]]
    bars_by_symbol: dict[str, list[dict]] = {}
    source = "alpaca"
    stale = False
    reason = None
    broker = get_broker()
    if broker:
        for symbol in selected:
            try:
                bars = broker.get_bars(symbol, timeframe="1Day", limit=limit)
                if bars:
                    bars_by_symbol[symbol] = bars
                    continue
            except Exception as exc:
                logger.debug("bar fetch failed for %s: %s", symbol, exc)
    if not bars_by_symbol:
        source = "fallback"
        stale = True
        reason = "live market data unavailable; deterministic fallback used"
        for symbol in selected:
            quote = _market_quote(symbol)
            bars_by_symbol[symbol] = _fallback_bars(symbol, quote.get("price") if quote else None, limit)

    _nexosignal_cache["symbol_bars"] = bars_by_symbol
    _nexosignal_cache["source"] = source
    _nexosignal_cache["ts"] = now
    return bars_by_symbol, source, stale, reason


def _safe_account_snapshot() -> tuple[dict, list[dict], dict | None]:
    broker = get_broker()
    if not broker:
        return {}, [], None
    try:
        return broker.get_account(), broker.get_positions(), broker.get_clock()
    except Exception:
        return {}, [], None


def _pnl_payload() -> dict:
    account, positions, _ = _safe_account_snapshot()
    trades = list_trade_events(500)
    unrealized = sum(float(p.get("unrealized_pl") or 0) for p in positions)
    realized = sum(float(t.get("realized_pnl") or 0) for t in trades if t.get("realized_pnl") is not None)
    wins = sum(1 for t in trades if float(t.get("realized_pnl") or 0) > 0)
    losses = sum(1 for t in trades if float(t.get("realized_pnl") or 0) < 0)
    closed = wins + losses
    pnl_values = [float(t.get("realized_pnl") or 0) for t in trades]
    return {
        "portfolio_value": float(account.get("portfolio_value") or 0),
        "open_pnl": round(unrealized, 2),
        "realized_pnl": round(realized, 2),
        "daily_pnl": round(unrealized + realized, 2),
        "win_loss_ratio": round(wins / max(losses, 1), 2) if closed else 0,
        "win_rate": round((wins / closed) * 100, 2) if closed else 0,
        "average_r": round(sum(float(t.get("realized_risk_reward_ratio") or 0) for t in trades) / max(closed, 1), 2) if closed else 0,
        "max_drawdown": round(min(pnl_values), 2) if pnl_values else 0,
        "best_trade": round(max(pnl_values), 2) if pnl_values else 0,
        "worst_trade": round(min(pnl_values), 2) if pnl_values else 0,
        "current_exposure": round(sum(float(p.get("market_value") or 0) for p in positions), 2),
    }


# ═══════════════════════════════════════════════════════════ AUTH ═══════════

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        stored = config.DASHBOARD_USERS.get(username)
        if stored == "change-me":
            flash("Set DASHBOARD_PASSWORD (or DASHBOARD_USERS) in .env before using the dashboard.", "error")
            return render_template("login.html")
        if authenticate_user(username, password):
            session["logged_in"] = True
            session["username"] = username
            return redirect(request.args.get("next") or url_for("dashboard"))
        flash("Invalid username or password.", "error")
    return render_template("login.html")


@app.post("/logout")
@login_required
def logout():
    session.clear()
    return redirect(url_for("login"))


# ═══════════════════════════════════════════════════════ PAGE ROUTES ════════

def _broker_data():
    """Shared broker data fetch used by multiple pages."""
    broker = get_broker()
    account = clock = None
    positions: list[dict] = []
    open_orders: list[dict] = []
    connection_error = None
    if broker:
        try:
            account = broker.get_account()
            record_brokerage_connection(
                provider="alpaca",
                account_label="Configured Alpaca account",
                account_id=account.get("id"),
                environment=config.TRADING_MODE,
                status="connected",
                api_key_last4=config.ALPACA_API_KEY[-4:] if config.ALPACA_API_KEY else None,
                notes="Auto-captured from current .env credentials.",
            )
            clock = broker.get_clock()
            positions = broker.get_positions()
            open_orders = broker.get_orders("open")
        except BrokerError as e:
            connection_error = friendly_broker_error(str(e))
    else:
        connection_error = "Missing Alpaca API keys. Fill ALPACA_API_KEY and ALPACA_SECRET_KEY in .env."
    return broker, account, clock, positions, open_orders, connection_error


@app.route("/")
@app.route("/portfolio")
@login_required
def dashboard():
    """Portfolio page — main landing page."""
    broker, account, clock, positions, open_orders, connection_error = _broker_data()
    return render_template(
        "portfolio.html",
        account=account,
        clock=clock,
        positions=positions,
        open_orders=open_orders,
        connection_error=connection_error,
        agent=current_agent_status(),
        trades=list_trade_events(50),
        summary=trade_summary(),
        wallet=latest_wallet_connection(),
        signals=list_signal_events(50),
        signal_summary=signal_summary(),
        config=config,
    )


@app.route("/markets")
@login_required
def markets():
    """Markets page — US equities, Asian markets, crypto."""
    broker = get_broker()
    clock = None
    if broker:
        try:
            clock = broker.get_clock()
        except BrokerError:
            pass

    # Fetch indices for the index strip at page load
    indices = _build_index_strip()

    return render_template(
        "markets.html",
        clock=clock,
        indices=indices,
        config=config,
    )


@app.route("/research")
@login_required
def research():
    """Research page — AI agents + intelligence panel."""
    return render_template(
        "research.html",
        watchlist=list_watchlist(30),
        macro=latest_macro_snapshot(),
        insiders=list_insider_activity(10),
        risk_metrics=list_risk_metrics(20),
        performance_events=list_performance_events(25),
        prefill_symbol=request.args.get("symbol", ""),
        config=config,
    )


@app.route("/trade")
@login_required
def trade():
    """Trade page — algo runner, manual orders, paper/live toggle."""
    broker, account, clock, positions, open_orders, connection_error = _broker_data()
    return render_template(
        "trade.html",
        account=account,
        clock=clock,
        positions=positions,
        open_orders=open_orders,
        connection_error=connection_error,
        agent=current_agent_status(),
        wallet=latest_wallet_connection(),
        brokerages=list_brokerage_connections(),
        config=config,
        strategies=sorted(STRATEGIES.keys()),
        prefill_symbol=request.args.get("symbol", ""),
        prefill_side=request.args.get("side", "buy"),
    )


@app.get("/health")
def health():
    return jsonify({
        "ok": True,
        "service": "NexoSignal",
        "mode": config.TRADING_MODE,
        "paper_default": config.ALPACA_PAPER,
        "live_trading_enabled": config.LIVE_TRADING,
        "autonomous_trading_enabled": config.AUTONOMOUS_TRADING,
        "storage_enabled": bool(config.SUPABASE_DB_URL),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


@app.get("/system-health")
@login_required
def system_health_page():
    tracker = get_tracker()
    return render_template(
        "health.html",
        config=config,
        status={
            "service": "NexoSignal",
            "mode": config.TRADING_MODE,
            "paper_default": config.ALPACA_PAPER,
            "live_trading_enabled": config.LIVE_TRADING,
            "autonomous_trading_enabled": config.AUTONOMOUS_TRADING,
            "manual_approval_required": config.REQUIRE_MANUAL_APPROVAL,
            "storage_enabled": bool(config.SUPABASE_DB_URL),
            "telegram_enabled": bool(config.TELEGRAM_TOKEN and config.TELEGRAM_CHAT_ID),
            "local_llm_enabled": config.LOCAL_LLM_ENABLED,
            "circuit_breaker_active": tracker.circuit_breaker_active,
            "circuit_breaker_reason": tracker.circuit_breaker_reason,
            "guard_strikes": tracker.guard_strikes,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
    )


@app.get("/api/nexosignal/status")
@login_required
def api_nexosignal_status():
    tracker = get_tracker()
    status = current_agent_status()
    return jsonify({
        "status": "ok",
        "agent": status,
        "safety": {
            "paper_mode": config.ALPACA_PAPER or config.TRADING_MODE == "paper",
            "live_trading_enabled": config.LIVE_TRADING,
            "autonomous_trading_enabled": config.AUTONOMOUS_TRADING,
            "manual_approval_required": config.REQUIRE_MANUAL_APPROVAL,
            "min_confidence": config.SIGNAL_MIN_CONFIDENCE,
            "min_risk_reward": config.MIN_RISK_REWARD_RATIO,
            "circuit_breaker_active": tracker.circuit_breaker_active or bool(status.get("circuit_breaker_active")),
            "circuit_breaker_reason": tracker.circuit_breaker_reason,
            "guard_strikes": tracker.guard_strikes,
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


@app.get("/api/nexosignal/top-predictions")
@login_required
def api_nexosignal_top_predictions():
    bars, source, stale, reason = _load_symbol_bars()
    _, _, clock = _safe_account_snapshot()
    market_open = bool(clock and clock.get("is_open", False))
    preds = build_ranked_predictions(
        bars,
        top_n=3,
        market_open=market_open,
        circuit_breaker_active=get_tracker().circuit_breaker_active,
    )
    return jsonify({
        "status": "ok" if not stale else "degraded",
        "stale_data": stale,
        "reason": reason,
        "source": source,
        "predictions": [asdict(p) for p in preds],
    })


@app.get("/api/nexosignal/alpha-picks")
@login_required
def api_nexosignal_alpha_picks():
    bars, source, stale, reason = _load_symbol_bars()
    picks = build_alpha_picks(bars, top_n=5)
    return jsonify({
        "status": "ok" if not stale else "degraded",
        "stale_data": stale,
        "reason": reason,
        "source": source,
        "alpha_picks": [asdict(p) for p in picks],
    })


@app.get("/api/nexosignal/risk-reward/<symbol>")
@login_required
def api_nexosignal_risk_reward(symbol: str):
    normalized = normalize_symbol(symbol)
    bars, source, stale, reason = _load_symbol_bars([normalized])
    item = bars.get(normalized)
    if not item:
        return jsonify({"status": "error", "stale_data": True, "reason": f"No bars for {normalized}"}), 404
    account, _, clock = _safe_account_snapshot()
    suite = evaluate_strategy_suite(normalized, item)
    best = sorted(suite, key=lambda s: (s.blocked_reason is None, s.expected_value_score, s.confluence_score), reverse=True)[0]
    rr = analyze_risk_reward(
        normalized,
        item,
        best.direction,
        max_risk_per_trade=min(config.MAX_ORDER_VALUE_USD, 100.0),
        buying_power=float(account.get("buying_power") or 0) if account else None,
        market_open=bool(clock and clock.get("is_open", False)),
        circuit_breaker_active=get_tracker().circuit_breaker_active,
        stale_data=stale,
    )
    return jsonify({
        "status": "ok" if not stale else "degraded",
        "stale_data": stale,
        "reason": reason,
        "source": source,
        "risk_reward": asdict(rr),
        "strategy_suite": [asdict(s) for s in suite],
    })


@app.get("/api/nexosignal/history/<symbol>")
@login_required
def api_nexosignal_history(symbol: str):
    normalized = normalize_symbol(symbol)
    bars, source, stale, reason = _load_symbol_bars([normalized], limit=120)
    item = bars.get(normalized, [])
    indicators = compute_indicators(item) if item else None
    return jsonify({
        "status": "ok" if item else "error",
        "stale_data": stale,
        "reason": reason,
        "source": source,
        "symbol": normalized,
        "bars": item[-120:],
        "indicators": asdict(indicators) if indicators else None,
    })


@app.get("/api/nexosignal/strategy-performance")
@login_required
def api_nexosignal_strategy_performance():
    trades = list_trade_events(500)
    events = list_performance_events(500)
    grouped: dict[str, dict] = {}
    for trade in trades:
        name = trade.get("strategy") or "manual"
        row = grouped.setdefault(name, {"strategy_name": name, "signals": 0, "trades": 0, "wins": 0, "losses": 0, "total_pnl": 0.0})
        row["trades"] += 1
        pnl = float(trade.get("realized_pnl") or 0)
        row["total_pnl"] += pnl
        row["wins"] += 1 if pnl > 0 else 0
        row["losses"] += 1 if pnl < 0 else 0
    for signal in list_signal_events(500):
        name = signal.get("strategy") or "unknown"
        grouped.setdefault(name, {"strategy_name": name, "signals": 0, "trades": 0, "wins": 0, "losses": 0, "total_pnl": 0.0})
        grouped[name]["signals"] += 1
    rows = []
    for row in grouped.values():
        closed = row["wins"] + row["losses"]
        rows.append({
            **row,
            "win_rate": round((row["wins"] / closed) * 100, 2) if closed else 0,
            "average_r": 0,
            "historical_confidence": 0,
            "current_status": "active" if row["signals"] or row["trades"] else "idle",
        })
    return jsonify({"status": "ok", "performance": rows, "telemetry_events": events[:25]})


@app.get("/api/nexosignal/positions")
@login_required
def api_nexosignal_positions():
    account, positions, clock = _safe_account_snapshot()
    return jsonify({
        "status": "ok" if account else "degraded",
        "market_open": bool(clock and clock.get("is_open", False)),
        "positions": positions,
    })


@app.get("/api/nexosignal/pnl")
@login_required
def api_nexosignal_pnl():
    return jsonify({"status": "ok", "pnl": _pnl_payload()})


@app.get("/api/nexosignal/telemetry")
@login_required
def api_nexosignal_telemetry():
    bars, source, stale, reason = _load_symbol_bars()
    predictions = build_ranked_predictions(bars, top_n=3)
    picks = build_alpha_picks(bars, top_n=5)
    tracker = get_tracker()
    return jsonify({
        "status": "ok" if not stale else "degraded",
        "stale_data": stale,
        "reason": reason,
        "source": source,
        "agent": current_agent_status(),
        "safety": {
            "circuit_breaker_active": tracker.circuit_breaker_active,
            "circuit_breaker_reason": tracker.circuit_breaker_reason,
            "guard_strikes": tracker.guard_strikes,
        },
        "top_predictions": [asdict(p) for p in predictions],
        "alpha_picks": [asdict(p) for p in picks],
        "pnl": _pnl_payload(),
        "alerts": list_performance_events(10),
    })


@app.post("/api/nexosignal/alerts/test")
@login_required
def api_nexosignal_alerts_test():
    from trading_agent.broker import TelegramNotifier

    try:
        TelegramNotifier().send("NexoSignal test alert: dashboard notification path is configured.")
        return jsonify({"ok": True, "provider": "telegram", "message": "test alert attempted"})
    except Exception as exc:
        logger.warning("Alert test failed: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 503


# ═══════════════════════════════════════════════ RESEARCH / EXPORT ROUTES ════

@app.get("/api/research/earnings")
@login_required
def api_research_earnings():
    from datetime import date, timedelta
    syms_raw = request.args.get("symbols", ",".join(config.DEFAULT_DASHBOARD_SYMBOLS[:10]))
    symbols = [normalize_symbol(s) for s in syms_raw.split(",") if s.strip()]
    cache_key = ",".join(sorted(symbols))
    now = time.time()
    if (_earnings_cache["data"] and now - _earnings_cache["ts"] < _EARNINGS_TTL
            and _earnings_cache["key"] == cache_key):
        return jsonify(_earnings_cache["data"])
    if not config.FMP_API_KEY:
        return jsonify({"status": "disabled", "reason": "FMP_API_KEY not configured", "earnings": []})
    today = date.today()
    from_dt = today.strftime("%Y-%m-%d")
    to_dt = (today + timedelta(days=30)).strftime("%Y-%m-%d")
    try:
        resp = requests.get(
            "https://financialmodelingprep.com/api/v3/earning_calendar",
            params={"from": from_dt, "to": to_dt, "apikey": config.FMP_API_KEY},
            timeout=8,
        )
        if not resp.ok:
            return jsonify({"status": "error", "reason": f"FMP {resp.status_code}", "earnings": []})
        sym_set = {s.split("/")[0] for s in symbols}
        data = resp.json() if isinstance(resp.json(), list) else []
        filtered = [
            {"symbol": e.get("symbol"), "date": e.get("date"),
             "eps_estimate": e.get("epsEstimated"), "revenue_estimate": e.get("revenueEstimated"),
             "time": e.get("time")}
            for e in data if e.get("symbol") in sym_set
        ]
        result = {"status": "ok", "earnings": filtered, "from": from_dt, "to": to_dt}
        _earnings_cache.update({"data": result, "ts": now, "key": cache_key})
        return jsonify(result)
    except Exception as exc:
        logger.warning("Earnings calendar failed: %s", exc)
        return jsonify({"status": "error", "reason": str(exc)[:120], "earnings": []})


@app.get("/api/research/sentiment/<symbol>")
@login_required
def api_research_sentiment(symbol: str):
    sym = normalize_symbol(symbol).replace("/", "").upper()
    cache_key = f"st:{sym}"
    now = time.time()
    cached = _quote_cache.get(cache_key)
    if cached and now - cached.get("_ts", 0) < 300:
        return jsonify({k: v for k, v in cached.items() if k != "_ts"})
    try:
        resp = requests.get(
            f"https://api.stocktwits.com/api/2/streams/symbol/{sym}.json",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=6,
        )
        if resp.status_code == 429:
            return jsonify({"status": "rate_limited", "symbol": sym, "bullish": 0, "bearish": 0, "total": 0})
        if not resp.ok:
            return jsonify({"status": "error", "symbol": sym, "reason": f"HTTP {resp.status_code}",
                            "bullish": 0, "bearish": 0, "total": 0})
        messages = resp.json().get("messages", [])
        bullish = sum(1 for m in messages
                      if (m.get("entities") or {}).get("sentiment", {}).get("basic") == "Bullish")
        bearish = sum(1 for m in messages
                      if (m.get("entities") or {}).get("sentiment", {}).get("basic") == "Bearish")
        total = len(messages)
        result = {"status": "ok", "symbol": sym, "bullish": bullish, "bearish": bearish, "total": total,
                  "bull_pct": round(bullish / max(total, 1) * 100),
                  "bear_pct": round(bearish / max(total, 1) * 100)}
        _quote_cache[cache_key] = {**result, "_ts": now}
        return jsonify(result)
    except Exception as exc:
        logger.warning("StockTwits failed for %s: %s", sym, exc)
        return jsonify({"status": "error", "symbol": sym, "reason": str(exc)[:120],
                        "bullish": 0, "bearish": 0, "total": 0})


@app.get("/api/research/dcf/<symbol>")
@login_required
def api_research_dcf(symbol: str):
    sym = normalize_symbol(symbol)
    if is_supported_crypto(sym):
        return jsonify({"status": "not_applicable", "reason": "DCF not available for crypto"})
    if not config.FMP_API_KEY:
        return jsonify({"status": "disabled", "reason": "FMP_API_KEY not configured"})
    cache_key = f"dcf:{sym}"
    now = time.time()
    cached = _quote_cache.get(cache_key)
    if cached and now - cached.get("_ts", 0) < 3600:
        return jsonify({k: v for k, v in cached.items() if k != "_ts"})
    try:
        resp = requests.get(
            f"https://financialmodelingprep.com/api/v3/discounted-cash-flow/{sym}",
            params={"apikey": config.FMP_API_KEY}, timeout=8,
        )
        if not resp.ok:
            return jsonify({"status": "error", "reason": f"FMP {resp.status_code}"})
        raw = resp.json()
        entry = (raw[0] if isinstance(raw, list) and raw else raw) or {}
        dcf_val = entry.get("dcf")
        price = entry.get("Stock Price") or entry.get("price")
        upside = round((float(dcf_val) / float(price) - 1) * 100, 1) if dcf_val and price else None
        result = {
            "status": "ok", "symbol": sym,
            "dcf_value": round(float(dcf_val), 2) if dcf_val else None,
            "current_price": round(float(price), 2) if price else None,
            "upside_pct": upside,
            "fair_value_label": ("undervalued" if upside and upside > 10
                                 else "overvalued" if upside and upside < -10
                                 else "fairly valued" if upside is not None else "unknown"),
            "date": entry.get("date"),
        }
        _quote_cache[cache_key] = {**result, "_ts": now}
        return jsonify(result)
    except Exception as exc:
        logger.warning("DCF failed for %s: %s", sym, exc)
        return jsonify({"status": "error", "reason": str(exc)[:120]})


@app.get("/api/export/bars/<symbol>")
@login_required
def api_export_bars(symbol: str):
    from flask import Response as FlaskResponse
    normalized = normalize_symbol(symbol)
    timeframe = request.args.get("timeframe", "1Day")
    limit = min(int(request.args.get("limit", "365")), 1000)
    broker = get_broker()
    if not broker:
        return jsonify({"error": "Broker unavailable"}), 503
    try:
        bars = broker.get_bars(normalized, timeframe=timeframe, limit=limit)
    except BrokerError as exc:
        return jsonify({"error": str(exc)}), 503
    if not bars:
        return jsonify({"error": f"No bars for {normalized}"}), 404
    lines = ["timestamp,open,high,low,close,volume"]
    for b in bars:
        lines.append(f"{b.get('t','')},{b.get('o',0)},{b.get('h',0)},{b.get('l',0)},{b.get('c',0)},{b.get('v',0)}")
    filename = f"{normalized.replace('/', '')}_{timeframe}_{limit}bars.csv"
    return FlaskResponse(
        "\n".join(lines), mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.get("/api/research/patterns/<symbol>")
@login_required
def api_research_patterns(symbol: str):
    normalized = normalize_symbol(symbol)
    bars, source, stale, reason = _load_symbol_bars([normalized], limit=50)
    item = bars.get(normalized, [])
    if len(item) < 10:
        return jsonify({"status": "error", "reason": f"insufficient bars for {normalized}",
                        "patterns": []}), 404
    try:
        import pandas as pd
        import pandas_ta as pta  # noqa: F401
        df = pd.DataFrame(item)
        df = df.rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
        for col in ("open", "high", "low", "close"):
            df[col] = pd.to_numeric(df[col], errors="coerce")
        cdl = df.ta.cdl_pattern(name="all", append=False)
        patterns = []
        if cdl is not None and len(cdl):
            last = cdl.iloc[-1]
            for col in cdl.columns:
                val = last[col]
                if val != 0:
                    label = col.replace("CDL_", "").replace("_", " ").title()
                    patterns.append({"pattern": label, "signal": "bullish" if val > 0 else "bearish"})
        return jsonify({"status": "ok", "symbol": normalized, "source": source,
                        "stale_data": stale, "patterns": patterns, "bar_count": len(item)})
    except ImportError:
        return jsonify({"status": "unavailable",
                        "reason": "pandas-ta not installed — run: pip install pandas-ta",
                        "patterns": []}), 503
    except Exception as exc:
        logger.exception("Pattern detection failed for %s", normalized)
        return jsonify({"status": "error", "reason": str(exc)[:200], "patterns": []}), 500


@app.get("/api/research/sector-allocation")
@login_required
def api_research_sector_allocation():
    _, positions, _ = _safe_account_snapshot()
    if not positions:
        return jsonify({"status": "ok", "sectors": [], "total": 0.0, "source": "none"})
    from trading_agent.restrictions import _SECTOR_BUCKETS
    sym_to_sector: dict[str, str] = {
        sym: bucket.replace("_", " ").title()
        for bucket, syms in _SECTOR_BUCKETS.items()
        for sym in syms
    }
    sector_values: dict[str, float] = {}
    for p in positions:
        sym = str(p.get("symbol", "")).upper()
        sector = sym_to_sector.get(sym, "Other")
        mv = float(p.get("market_value") or 0)
        sector_values[sector] = sector_values.get(sector, 0.0) + mv
    total = sum(sector_values.values())
    sectors = sorted(
        [{"sector": s, "value": round(v, 2), "pct": round(v / max(total, 0.01) * 100, 1)}
         for s, v in sector_values.items()],
        key=lambda x: x["value"], reverse=True,
    )
    return jsonify({"status": "ok", "sectors": sectors, "total": round(total, 2),
                    "source": "alpaca+static"})


@app.get("/api/research/analyst/<symbol>")
@login_required
def api_research_analyst(symbol: str):
    sym = normalize_symbol(symbol)
    if is_supported_crypto(sym):
        return jsonify({"status": "not_applicable", "reason": "No analyst ratings for crypto",
                        "ratings": []})
    if not config.FMP_API_KEY:
        return jsonify({"status": "disabled", "reason": "FMP_API_KEY not configured", "ratings": []})
    cache_key = f"analyst:{sym}"
    now = time.time()
    cached = _quote_cache.get(cache_key)
    if cached and now - cached.get("_ts", 0) < 3600:
        return jsonify({k: v for k, v in cached.items() if k != "_ts"})
    try:
        resp = requests.get(
            f"https://financialmodelingprep.com/api/v3/upgrades-downgrades/{sym}",
            params={"apikey": config.FMP_API_KEY}, timeout=8,
        )
        if not resp.ok:
            return jsonify({"status": "error", "reason": f"FMP {resp.status_code}", "ratings": []})
        raw = resp.json()
        ratings = [
            {"published_date": r.get("publishedDate", "")[:10], "firm": r.get("gradingCompany"),
             "action": r.get("action"), "from_grade": r.get("previousGrade"),
             "to_grade": r.get("newGrade")}
            for r in (raw if isinstance(raw, list) else [])[:15]
        ]
        result = {"status": "ok", "symbol": sym, "ratings": ratings}
        _quote_cache[cache_key] = {**result, "_ts": now}
        return jsonify(result)
    except Exception as exc:
        logger.warning("Analyst ratings failed for %s: %s", sym, exc)
        return jsonify({"status": "error", "reason": str(exc)[:120], "ratings": []})


# ═══════════════════════════════════════════════════════ TRADING ACTIONS ════

@app.post("/wallet/metamask")
@login_required
def connect_metamask():
    data = request.get_json(silent=True) or {}
    address = str(data.get("address", "")).strip().lower()
    if not address.startswith("0x") or len(address) != 42:
        return jsonify({"ok": False, "error": "Invalid wallet address."}), 400
    record_wallet_connection(address, request.headers.get("User-Agent"))
    return jsonify({"ok": True, "address": address})


@app.post("/brokerages")
@login_required
def add_brokerage_connection():
    provider       = request.form.get("provider", "").strip().lower()
    account_label  = request.form.get("account_label", "").strip()
    account_id     = request.form.get("account_id", "").strip() or None
    environment    = request.form.get("environment", "paper").strip().lower()
    status         = request.form.get("status", "manual").strip().lower()
    api_key_last4  = request.form.get("api_key_last4", "").strip() or None
    notes          = request.form.get("notes", "").strip() or None
    if not provider or not account_label:
        flash("Provider and account label are required.", "error")
        return redirect(url_for("trade"))
    record_brokerage_connection(
        provider=provider, account_label=account_label, account_id=account_id,
        environment=environment, status=status, api_key_last4=api_key_last4, notes=notes,
    )
    flash("Brokerage connection saved.", "success")
    return redirect(url_for("trade"))


@app.post("/manual-order")
@login_required
def manual_order():
    symbol  = request.form.get("symbol", "").strip().upper()
    side    = request.form.get("side", "").strip().lower()
    qty     = float(request.form.get("qty", "0"))
    dry_run = request.form.get("dry_run") == "on"
    if not symbol or side not in {"buy", "sell"} or qty <= 0:
        flash("Enter a valid symbol, side, and quantity.", "error")
        return redirect(url_for("trade"))
    try:
        broker = AlpacaBroker()
        price = broker.get_last_price(symbol)
        if dry_run:
            record_trade_event(
                source="manual", symbol=symbol, side=side, qty=qty,
                order_type="market", status="dry_run", price=price, dry_run=True,
                execution_mode="manual",
            )
            flash(f"Dry run recorded: {side.upper()} {qty} {symbol}.", "success")
            return redirect(url_for("trade"))
        order = broker.place_market_order(symbol, side, qty)
        record_trade_event(
            source="manual", symbol=symbol, side=side, qty=qty,
            order_type="market", status=order.get("status", "submitted"),
            order_id=order.get("id"), price=price, raw=order, execution_mode="manual",
        )
        # Start background thread to track when this order fills
        order_id_val = order.get("id")
        if order_id_val:
            t = threading.Thread(
                target=_poll_order_fill,
                args=(order_id_val, symbol),
                daemon=True,
            )
            t.start()
        flash(f"Order submitted: {side.upper()} {qty} {symbol}.", "success")
    except (BrokerError, RejectedOrder, ValueError) as e:
        record_trade_event(
            source="manual", symbol=symbol or "?", side=side or "?",
            qty=qty if qty > 0 else 0, order_type="market", status="error",
            error=str(e), execution_mode="manual",
        )
        flash(f"Order failed: {e}", "error")
    return redirect(url_for("trade"))


@app.post("/bot/start")
@login_required
def start_bot():
    global _runtime
    if config.RUNNING_ON_VERCEL:
        flash("Bot loops cannot run on Vercel serverless.", "error")
        return redirect(url_for("trade"))
    symbols = [s.strip().upper() for s in request.form.get("symbols", "").split(",") if s.strip()] or config.DEFAULT_DASHBOARD_SYMBOLS
    strategy = request.form.get("strategy", "sma_crossover")
    qty      = float(request.form.get("qty", "1"))
    interval = int(request.form.get("interval", "60"))
    dry_run  = request.form.get("dry_run") == "on"
    with _agent_lock:
        if _runtime and _runtime.is_running:
            flash("Bot is already running.", "error")
            return redirect(url_for("trade"))
        connection_error = validate_alpaca_connection()
        if connection_error:
            flash(f"Could not start bot: {friendly_broker_error(connection_error)}", "error")
            return redirect(url_for("trade"))
        try:
            agent = NexoSignalAgent(
                symbols=symbols, strategy_name=strategy,
                qty_per_trade=qty, poll_interval_sec=interval, dry_run=dry_run,
            )
            _runtime = AgentRuntime(agent)
            _runtime.start()
        except (BrokerError, ValueError) as e:
            flash(f"Could not start bot: {e}", "error")
            return redirect(url_for("trade"))
        except Exception as e:
            _runtime = None
            flash(f"Could not start runtime: {e}", "error")
            return redirect(url_for("trade"))
    flash("Bot started.", "success")
    return redirect(url_for("trade"))


@app.post("/bot/stop")
@login_required
def stop_bot():
    global _runtime
    with _agent_lock:
        runtime = _runtime
    if runtime:
        runtime.stop(timeout=10)
    with _agent_lock:
        if _runtime is runtime:
            _runtime = None
    flash("Bot stop requested.", "success")
    return redirect(url_for("trade"))


@app.post("/orders/<order_id>/cancel")
@login_required
def cancel_order(order_id: str):
    try:
        AlpacaBroker().cancel_order(order_id)
        flash(f"Cancelled order {order_id}.", "success")
    except BrokerError as e:
        flash(f"Cancel failed: {e}", "error")
    return redirect(url_for("dashboard"))


@app.get("/api/orders/<order_id>/status")
@login_required
def api_order_status(order_id: str):
    """Poll Alpaca for a single order's fill status."""
    try:
        order = AlpacaBroker().get_order(order_id)
        return jsonify({
            "id":          order.get("id"),
            "status":      order.get("status"),
            "filled_qty":  order.get("filled_qty"),
            "filled_avg_price": order.get("filled_avg_price"),
            "symbol":      order.get("symbol"),
            "side":        order.get("side"),
            "qty":         order.get("qty"),
            "updated_at":  order.get("updated_at"),
        })
    except BrokerError as exc:
        return jsonify({"error": str(exc)}), 503


def _poll_order_fill(order_id: str, symbol: str, max_attempts: int = 12) -> None:
    """Background thread: poll Alpaca until order fills or times out, then emit SocketIO."""
    for _ in range(max_attempts):
        time.sleep(5)
        try:
            order = AlpacaBroker().get_order(order_id)
            status = order.get("status", "")
            if status in ("filled", "partially_filled", "canceled", "expired", "rejected"):
                filled_qty_val   = order.get("filled_qty")
                filled_price_val = order.get("filled_avg_price")
                logger.info(
                    "Order %s for %s reached terminal status: %s (qty=%s price=%s)",
                    order_id, symbol, status, filled_qty_val, filled_price_val,
                )
                # Persist updated status back to our database
                try:
                    update_trade_event_status(
                        order_id=order_id,
                        status=status,
                        filled_qty=float(filled_qty_val) if filled_qty_val else None,
                        filled_avg_price=float(filled_price_val) if filled_price_val else None,
                    )
                except Exception:
                    pass
                publish_realtime_event("order_fill", {
                    "order_id":         order_id,
                    "symbol":           symbol,
                    "status":           status,
                    "filled_qty":       filled_qty_val,
                    "filled_avg_price": filled_price_val,
                })
                return
        except Exception:
            pass  # network hiccup — keep polling


# ═══════════════════════════════════════════════ JSON API — MODE TOGGLE ════

@app.post("/api/toggle-mode")
@login_required
def api_toggle_mode():
    """Switch between paper and live trading modes at runtime."""
    data = request.get_json(silent=True) or {}
    mode = data.get("mode", "paper")
    if mode not in ("paper", "live"):
        return jsonify({"error": "mode must be 'paper' or 'live'"}), 400
    set_trading_mode(mode)
    logger.warning("Trading mode switched to: %s by user=%s", mode, session.get("username"))
    return jsonify({"mode": mode, "ok": True})


@app.post("/webhooks/tradingview")
def tradingview_webhook():
    """Receive TradingView alerts as market-data/signal overlays."""
    data = request.get_json(silent=True) or {}
    expected_secret = getattr(config, "TRADINGVIEW_WEBHOOK_SECRET", "")
    provided_secret = (
        data.get("secret")
        or request.headers.get("X-TradingView-Secret")
        or request.args.get("secret")
    )
    if expected_secret and provided_secret != expected_secret:
        return jsonify({"ok": False, "error": "invalid secret"}), 401

    symbol = str(data.get("symbol") or data.get("ticker") or "").strip().upper()
    if not symbol:
        return jsonify({"ok": False, "error": "symbol required"}), 400
    symbol = _public_symbol(symbol)

    raw_price = data.get("price") or data.get("close")
    try:
        price = float(raw_price) if raw_price is not None else None
    except (TypeError, ValueError):
        price = None

    signal = str(data.get("signal") or data.get("side") or "hold").lower()
    if signal not in {"buy", "sell", "hold"}:
        signal = "hold"
    confidence = float(data.get("confidence") or data.get("score") or 50)
    payload = {
        "symbol": symbol,
        "price": round(price, 6) if price is not None else None,
        "change": data.get("change"),
        "change_pct": data.get("change_pct"),
        "signal": signal,
        "confidence": confidence,
        "timeframe": data.get("timeframe"),
        "source": "tradingview",
        "provider": "tradingview",
        "asof": datetime.now(timezone.utc).isoformat(),
        "_ts": time.time(),
        "stale": False,
    }

    with _tradingview_lock:
        _tradingview_quotes[symbol] = payload
    for key in list(_quote_cache):
        if key.startswith(f"{symbol}:"):
            _quote_cache.pop(key, None)

    try:
        record_signal_event(
            symbol=symbol,
            strategy="TradingView Webhook",
            base_signal=signal,
            final_signal=signal,
            confidence=confidence,
            approved=confidence >= config.SIGNAL_MIN_CONFIDENCE,
            reason=str(data.get("message") or data.get("reason") or "TradingView webhook alert"),
            price=price or 0.0,
            indicators={k: v for k, v in data.items() if k != "secret"},
            confluence_score=confidence,
            order_book_imbalance=0.0,
        )
    except Exception:
        logger.exception("TradingView webhook could not write signal event")

    publish_realtime_event("tradingview_signal", {k: v for k, v in payload.items() if k != "_ts"})
    return jsonify({"ok": True, "symbol": symbol, "source": "tradingview"})


# ═══════════════════════════════════════════════ JSON API — TICKER STRIP ════

# Simple in-memory cache so we don't hammer Alpaca on every page load
_ticker_cache: dict = {"data": None, "ts": 0}
_TICKER_TTL = 30  # seconds
_quote_cache: dict[str, dict] = {}
_QUOTE_TTL = 15
_earnings_cache: dict = {"data": None, "ts": 0.0, "key": ""}
_EARNINGS_TTL = 3600
_tradingview_quotes: dict[str, dict] = {}
_tradingview_lock = threading.Lock()

# OHLCV cache for the chart page (avoids hammering Alpaca on every indicator toggle)
_chart_ohlcv_cache: dict[str, dict] = {}  # key → {data, ts}

# Lens intelligence summary cache (15 min TTL — avoids re-running AI on every page view)
_lens_summary_cache: dict[str, dict] = {}  # symbol → {data, ts}
_LENS_SUMMARY_TTL = 900  # 15 minutes
_CHART_OHLCV_TTL: dict[str, int] = {  # TTL seconds by timeframe
    "1Min": 30, "5Min": 30, "15Min": 60, "30Min": 60,
    "1Hour": 120, "4Hour": 300,
    "1Day": 300, "1Week": 600, "1Month": 1800,
}
_DEFAULT_CHART_TTL = 60
_ALLOWED_TIMEFRAMES = frozenset(_CHART_OHLCV_TTL.keys())

_TICKER_SYMBOLS = [
    # US Indices (ETFs)
    {"symbol": "SPY",  "label": "S&P 500"},
    {"symbol": "QQQ",  "label": "Nasdaq"},
    {"symbol": "DIA",  "label": "Dow 30"},
    {"symbol": "IWM",  "label": "Russell 2K"},
    # Crypto
    {"symbol": "BTC/USD", "label": "Bitcoin"},
    {"symbol": "ETH/USD", "label": "Ethereum"},
    {"symbol": "SOL/USD", "label": "Solana"},
    # US stocks
    {"symbol": "AAPL",  "label": "Apple"},
    {"symbol": "NVDA",  "label": "NVIDIA"},
    {"symbol": "TSLA",  "label": "Tesla"},
    {"symbol": "MSFT",  "label": "Microsoft"},
    {"symbol": "AMZN",  "label": "Amazon"},
    {"symbol": "META",  "label": "Meta"},
    {"symbol": "GOOGL", "label": "Alphabet"},
]


def _provider_order() -> list[str]:
    order = [
        p.strip().lower()
        for p in getattr(config, "MARKET_DATA_PROVIDER", "alpaca,yahoo,tradingview").split(",")
        if p.strip()
    ]
    return order or ["alpaca", "yahoo", "tradingview"]


def _public_symbol(symbol: str) -> str:
    return normalize_symbol(symbol).upper()


def _yahoo_symbol(symbol: str) -> str:
    normalized = _public_symbol(symbol)
    if "/" in normalized:
        return normalized.replace("/", "-")
    return normalized


def _bars_to_quote(symbol: str, bars: list[dict], *, source: str, label: str | None = None) -> dict | None:
    if not bars:
        return None
    current_bar = bars[-1]
    cur = float(current_bar.get("c") or 0)
    if cur <= 0:
        return None
    prev = float(bars[-2].get("c") or cur) if len(bars) >= 2 else cur
    change = cur - prev
    change_pct = (change / prev * 100) if prev else 0.0
    return {
        "symbol": _public_symbol(symbol),
        "label": label or _public_symbol(symbol),
        "name": label or _public_symbol(symbol),
        "price": round(cur, 4),
        "change": round(change, 4),
        "change_pct": round(change_pct, 2),
        "volume": float(current_bar.get("v") or 0),
        "avg_volume": None,
        "market_cap": None,
        "pe_ratio": None,
        "wk52_high": None,
        "wk52_low": None,
        "wk52_chg": None,
        "source": source,
        "provider": source,
        "asof": datetime.now(timezone.utc).isoformat(),
        "stale": False,
    }


def _alpaca_quote(symbol: str, *, label: str | None = None) -> dict | None:
    try:
        broker = AlpacaBroker()
        normalized = _public_symbol(symbol)
        if is_supported_crypto(normalized):
            bars = broker.get_bars(normalized, timeframe="1Day", limit=2)
            return _bars_to_quote(normalized, bars, source="alpaca", label=label)

        snapshots = broker.get_bulk_snapshots_sync([normalized])
        snap = snapshots.get(normalized) or snapshots.get(normalized.upper())
        if snap:
            daily = snap.get("dailyBar") or {}
            prev_daily = snap.get("prevDailyBar") or {}
            latest_trade = snap.get("latestTrade") or {}
            cur = float(latest_trade.get("p") or daily.get("c") or 0)
            prev = float(prev_daily.get("c") or daily.get("o") or cur)
            if cur > 0:
                change = cur - prev
                change_pct = (change / prev * 100) if prev else 0.0
                return {
                    "symbol": normalized,
                    "label": label or normalized,
                    "name": label or normalized,
                    "price": round(cur, 4),
                    "change": round(change, 4),
                    "change_pct": round(change_pct, 2),
                    "volume": float(daily.get("v") or 0),
                    "avg_volume": None,
                    "market_cap": None,
                    "pe_ratio": None,
                    "wk52_high": None,
                    "wk52_low": None,
                    "wk52_chg": None,
                    "source": "alpaca",
                    "provider": "alpaca",
                    "asof": datetime.now(timezone.utc).isoformat(),
                    "stale": False,
                }
        bars = broker.get_bars(normalized, timeframe="1Day", limit=2)
        return _bars_to_quote(normalized, bars, source="alpaca", label=label)
    except Exception:
        return None


def _yahoo_quote(symbol: str, *, label: str | None = None) -> dict | None:
    quote = _fetch_yfinance_quote(_yahoo_symbol(symbol))
    if not quote:
        return None
    normalized = _public_symbol(symbol)
    quote.update({
        "symbol": normalized,
        "label": label or normalized,
        "name": label or normalized,
        "source": "yahoo",
        "provider": "yahoo",
        "asof": datetime.now(timezone.utc).isoformat(),
        "stale": False,
    })
    quote.setdefault("avg_volume", quote.get("volume"))
    quote.setdefault("market_cap", None)
    quote.setdefault("pe_ratio", None)
    quote.setdefault("wk52_chg", None)
    return quote


def _tradingview_quote(symbol: str, *, label: str | None = None) -> dict | None:
    normalized = _public_symbol(symbol)
    with _tradingview_lock:
        quote = dict(_tradingview_quotes.get(normalized) or {})
    if not quote:
        return None
    quote.setdefault("symbol", normalized)
    quote.setdefault("label", label or normalized)
    quote.setdefault("name", label or normalized)
    quote.setdefault("source", "tradingview")
    quote.setdefault("provider", "tradingview")
    quote["stale"] = (time.time() - float(quote.get("_ts", 0))) > 120
    return {k: v for k, v in quote.items() if k != "_ts"}


def _market_quote(symbol: str, *, label: str | None = None, use_cache: bool = True) -> dict | None:
    normalized = _public_symbol(symbol)
    cache_key = f"{normalized}:{label or ''}"
    now = time.time()
    if use_cache:
        cached = _quote_cache.get(cache_key)
        if cached and now - cached.get("_ts", 0) < _QUOTE_TTL:
            return {k: v for k, v in cached.items() if k != "_ts"}

    providers = {
        "alpaca": _alpaca_quote,
        "yahoo": _yahoo_quote,
        "tradingview": _tradingview_quote,
    }
    last_quote = None
    for provider_name in _provider_order():
        fetcher = providers.get(provider_name)
        if not fetcher:
            continue
        quote = fetcher(normalized, label=label)
        if quote:
            last_quote = quote
            if not quote.get("stale"):
                break

    if last_quote:
        cached = dict(last_quote)
        cached["_ts"] = now
        _quote_cache[cache_key] = cached
    return last_quote


@app.get("/api/ticker-strip")
@login_required
def api_ticker_strip():
    now = time.time()
    if _ticker_cache["data"] and (now - _ticker_cache["ts"]) < _TICKER_TTL:
        return jsonify(_ticker_cache["data"])

    tickers = []

    def _fetch_ticker(item):
        quote = _market_quote(item["symbol"], label=item["label"])
        if not quote:
            return None
        return {
            "symbol": item["label"],
            "raw_symbol": quote.get("symbol", item["symbol"]),
            "price": quote.get("price"),
            "change": quote.get("change"),
            "change_pct": quote.get("change_pct", 0.0),
            "source": quote.get("source", "unknown"),
            "asof": quote.get("asof"),
            "stale": bool(quote.get("stale")),
        }

    with ThreadPoolExecutor(max_workers=8) as pool:
        for row in pool.map(_fetch_ticker, _TICKER_SYMBOLS, timeout=15):
            if row:
                tickers.append(row)

    result = {"tickers": tickers, "provider_order": _provider_order(), "asof": datetime.now(timezone.utc).isoformat()}
    _ticker_cache["data"] = result
    _ticker_cache["ts"] = now
    return jsonify(result)


# ═══════════════════════════════════════════════ JSON API — MARKET DATA ════

# US stocks for the markets page tables
_US_MOST_ACTIVE = [
    "AAPL","NVDA","MSFT","AMZN","TSLA","META","GOOGL","AMD","INTC","SOFI",
    "MRVL","NOK","AAL","SPY","QQQ","BABA","BAC","JPM","GS","PLTR",
    "RIVN","LCID","F","GM","DIS","NFLX","ORCL","IBM","CRM","UBER",
]
_US_CRYPTO = [
    {"symbol": "BTC/USD", "name": "Bitcoin"},
    {"symbol": "ETH/USD", "name": "Ethereum"},
    {"symbol": "SOL/USD", "name": "Solana"},
    {"symbol": "DOGE/USD", "name": "Dogecoin"},
    {"symbol": "ADA/USD",  "name": "Cardano"},
    {"symbol": "XRP/USD",  "name": "XRP"},
    {"symbol": "AVAX/USD", "name": "Avalanche"},
    {"symbol": "LINK/USD", "name": "Chainlink"},
    {"symbol": "DOT/USD",  "name": "Polkadot"},
    {"symbol": "LTC/USD",  "name": "Litecoin"},
]
_ASIAN_INDICES = [
    {"symbol": "^N225",  "name": "Nikkei 225",    "exchange": "TSE"},
    {"symbol": "^HSI",   "name": "Hang Seng",      "exchange": "HKEX"},
    {"symbol": "000001.SS","name": "Shanghai Comp","exchange": "SSE"},
    {"symbol": "^BSESN", "name": "BSE Sensex",     "exchange": "BSE"},
    {"symbol": "^STI",   "name": "Straits Times",  "exchange": "SGX"},
    {"symbol": "^TWII",  "name": "TWSE",            "exchange": "TWSE"},
    {"symbol": "^KS11",  "name": "KOSPI",           "exchange": "KRX"},
    {"symbol": "^AXJO",  "name": "ASX 200",         "exchange": "ASX"},
    {"symbol": "^NSEI",  "name": "Nifty 50",        "exchange": "NSE"},
    {"symbol": "^KLSE",  "name": "FTSE Bursa MY",   "exchange": "Bursa"},
]

_market_cache: dict = {"data": {}, "ts": {}}
_MARKET_TTL = 60


def _fetch_yfinance_quote(symbol: str) -> dict | None:
    """Fetch a single quote from Yahoo Finance public API (no auth needed)."""
    try:
        import urllib.request, json as _json
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}?interval=1d&range=5d"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = _json.loads(resp.read())
        result = data["chart"]["result"][0]
        meta = result["meta"]
        closes = result["indicators"]["quote"][0].get("close", [])
        closes = [c for c in closes if c is not None]
        if len(closes) >= 2:
            prev = closes[-2]
            cur  = closes[-1]
        elif closes:
            prev = closes[-1]
            cur  = meta.get("regularMarketPrice", closes[-1])
        else:
            return None
        change = cur - prev
        change_pct = (change / prev * 100) if prev else 0.0
        return {
            "price": round(cur, 2),
            "change": round(change, 2),
            "change_pct": round(change_pct, 2),
            "volume": meta.get("regularMarketVolume"),
            "wk52_high": meta.get("fiftyTwoWeekHigh"),
            "wk52_low":  meta.get("fiftyTwoWeekLow"),
        }
    except Exception:
        return None


def _build_index_strip() -> list[dict]:
    indices = []
    for idx in _ASIAN_INDICES[:6]:
        q = _fetch_yfinance_quote(idx["symbol"])
        if q:
            indices.append({
                "name":       idx["name"],
                "region":     idx["exchange"],
                "price":      f"{q['price']:,.2f}",
                "change":     q["change"],
                "change_pct": q["change_pct"],
            })
    return indices


@app.get("/api/market-data")
@login_required
def api_market_data():
    region = request.args.get("region", "us")
    screen = request.args.get("screen", "active")
    cache_key = f"{region}:{screen}"
    now = time.time()

    if (_market_cache["data"].get(cache_key) and
            (now - _market_cache["ts"].get(cache_key, 0)) < _MARKET_TTL):
        return jsonify(_market_cache["data"][cache_key])

    result: dict = {"stocks": [], "asian": [], "crypto": []}

    if region == "us":
        def _fetch_stock(sym: str) -> dict | None:
            quote = _market_quote(sym)
            if not quote:
                return None
            quote["name"] = quote.get("name") or sym
            return quote

        try:
            with ThreadPoolExecutor(max_workers=10) as pool:
                for row in pool.map(_fetch_stock, _US_MOST_ACTIVE):
                    if row:
                        result["stocks"].append(row)
        except Exception as exc:
            logger.warning("Unified US market fetch failed: %s", exc)

        if screen == "gainers":
            result["stocks"].sort(key=lambda x: x.get("change_pct") or 0, reverse=True)
        elif screen == "losers":
            result["stocks"].sort(key=lambda x: x.get("change_pct") or 0)
        elif screen == "52wk_high":
            result["stocks"].sort(
                key=lambda x: x.get("wk52_chg") if x.get("wk52_chg") is not None else x.get("change_pct") or 0,
                reverse=True,
            )
        elif screen == "52wk_low":
            result["stocks"].sort(
                key=lambda x: x.get("wk52_chg") if x.get("wk52_chg") is not None else x.get("change_pct") or 0,
            )
        else:
            result["stocks"].sort(key=lambda x: x.get("volume") or 0, reverse=True)

    if region in ("us", "asia"):
        def _fetch_asian_unified(idx_item: dict) -> dict | None:
            quote = _market_quote(idx_item["symbol"], label=idx_item["name"])
            if not quote:
                return None
            quote["symbol"] = idx_item["symbol"]
            quote["name"] = idx_item["name"]
            quote["exchange"] = idx_item["exchange"]
            return quote

        try:
            with ThreadPoolExecutor(max_workers=5) as pool:
                for row in pool.map(_fetch_asian_unified, _ASIAN_INDICES):
                    if row:
                        result["asian"].append(row)
        except Exception as exc:
            logger.warning("Unified Asian market fetch failed: %s", exc)

    if region in ("us", "crypto"):
        def _fetch_crypto_unified(item: dict) -> dict | None:
            quote = _market_quote(item["symbol"], label=item["name"])
            if not quote:
                return None
            quote["symbol"] = _public_symbol(item["symbol"]).replace("/USD", "")
            quote["name"] = item["name"]
            quote["chg7d"] = quote.get("wk52_chg")
            return quote

        try:
            with ThreadPoolExecutor(max_workers=5) as pool:
                for row in pool.map(_fetch_crypto_unified, _US_CRYPTO):
                    if row:
                        result["crypto"].append(row)
        except Exception as exc:
            logger.warning("Unified crypto market fetch failed: %s", exc)
        result["crypto"].sort(key=lambda x: x.get("change_pct") or 0, reverse=True)

    result["provider_order"] = _provider_order()
    result["asof"] = datetime.now(timezone.utc).isoformat()
    _market_cache["data"][cache_key] = result
    _market_cache["ts"][cache_key] = now
    return jsonify(result)


# ═══════════════════════════════════════════════════════ JSON API — OTHER ════

@app.get("/api/chart-data")
@login_required
def api_chart_data():
    trades  = list_trade_events(500)
    signals = list_signal_events(200)
    conf_buckets = [0] * 10
    for s in signals:
        bucket = min(int(float(s.get("confidence", 0)) / 10), 9)
        conf_buckets[bucket] += 1
    approved_count = sum(1 for s in signals if s.get("approved"))
    recent = list(reversed(signals[:30]))
    signal_trend = [
        {"label": f"{s.get('symbol','')} {s.get('created_at','')[:10]}",
         "confidence": round(float(s.get("confidence", 0)), 1),
         "signal": s.get("final_signal", "hold"),
         "approved": bool(s.get("approved"))}
        for s in recent
    ]
    buy_n  = sum(1 for s in signals if s.get("final_signal") == "buy")
    sell_n = sum(1 for s in signals if s.get("final_signal") == "sell")
    hold_n = sum(1 for s in signals if s.get("final_signal") == "hold")
    strategy_counts: dict[str, int] = {}
    for t in trades:
        strat = t.get("strategy") or "manual"
        strategy_counts[strat] = strategy_counts.get(strat, 0) + 1
    return jsonify({
        "confidence_distribution": {
            "labels": ["0-10","10-20","20-30","30-40","40-50","50-60","60-70","70-80","80-90","90-100"],
            "data": conf_buckets,
        },
        "approval_stats":  {"approved": approved_count, "rejected": len(signals) - approved_count},
        "signal_direction": {"buy": buy_n, "sell": sell_n, "hold": hold_n},
        "signal_trend":    signal_trend,
        "strategy_counts": strategy_counts,
    })


@app.get("/api/live-metrics")
@login_required
def api_live_metrics():
    broker = get_broker()
    if not broker:
        return jsonify({"error": "Broker unavailable"}), 503
    try:
        account   = broker.get_account()
        positions = broker.get_positions()
        clock     = broker.get_clock()
        total_upl = sum(float(p.get("unrealized_pl", 0)) for p in positions)
        return jsonify({
            "portfolio_value": float(account.get("portfolio_value", 0)),
            "cash":            float(account.get("cash", 0)),
            "buying_power":    float(account.get("buying_power", 0)),
            "market_open":     bool(clock.get("is_open", False)),
            "total_unrealized_pl": round(total_upl, 2),
            "positions": [
                {"symbol": p["symbol"], "qty": float(p["qty"]),
                 "market_value": float(p["market_value"]),
                 "unrealized_pl": float(p["unrealized_pl"]),
                 "unrealized_plpc": round(float(p.get("unrealized_plpc", 0)) * 100, 2)}
                for p in positions
            ],
        })
    except BrokerError as exc:
        return jsonify({"error": str(exc)}), 503


@app.get("/api/agent-status")
@login_required
def api_agent_status():
    return jsonify(current_agent_status())


@app.get("/api/ai-research/<symbol>")
@login_required
def api_ai_research(symbol: str):
    if not config.AI_RESEARCH_ENABLED:
        return jsonify({"error": "AI Research disabled. Set AI_RESEARCH_ENABLED=true in .env."}), 503
    symbol = symbol.strip().upper()
    if not symbol:
        return jsonify({"error": "Symbol required"}), 400
    try:
        broker = AlpacaBroker()
        bars   = broker.get_bars(symbol, timeframe="1Min", limit=30)
        if not bars:
            return jsonify({"error": f"No bar data for {symbol}"}), 404
        price = float(bars[-1]["c"])
        from trading_agent.signal_engine import analyze_signal
        decision = analyze_signal(symbol, bars, "hold", config.SIGNAL_MIN_CONFIDENCE)
        indicators_dict = asdict(decision.indicators)
        from trading_agent.ai_research import run_multi_agent_research
        research = run_multi_agent_research(
            symbol=symbol, price=price, bars=bars,
            base_signal=decision.base_signal,
            base_confidence=decision.confidence,
            indicators=indicators_dict,
        )
        def _sig(s):
            if not s:
                return None
            return {"provider": s.provider, "signal": s.signal, "confidence": s.confidence,
                    "rationale": s.rationale, "price_target": s.price_target,
                    "stop_loss": s.stop_loss, "risk_reward_ratio": s.risk_reward_ratio,
                    "sentiment": s.sentiment, "timeframe": s.timeframe,
                    "news_catalyst": s.news_catalyst, "error": s.error}
        return jsonify({
            "symbol": research.symbol, "current_price": price,
            "gemini": _sig(research.gemini), "grok": _sig(research.grok),
            "claude": _sig(research.claude), "local_master": _sig(research.local_master),
            "local_llm_enabled": config.LOCAL_LLM_ENABLED, "ollama_model": config.OLLAMA_MODEL,
            "consensus_signal": research.consensus_signal,
            "consensus_confidence": research.consensus_confidence,
            "approved_5to1": research.approved_5to1,
            "entry_price": research.entry_price, "stop_loss": research.stop_loss,
            "take_profit": research.take_profit, "risk_reward_ratio": research.risk_reward_ratio,
            "min_ratio_required": config.AI_MIN_RISK_REWARD_RATIO,
            "master_decision": asdict(research.master_decision) if research.master_decision else None,
            "technical": {"base_signal": decision.base_signal, "final_signal": decision.final_signal,
                          "confidence": decision.confidence, "approved": decision.approved, "reason": decision.reason},
        })
    except BrokerError as exc:
        return jsonify({"error": f"Broker error: {exc}"}), 503
    except Exception as exc:
        logger.exception("AI research failed for %s", symbol)
        return jsonify({"error": str(exc)}), 500


@app.get("/api/price/<symbol>")
@login_required
def api_price(symbol: str):
    symbol = symbol.strip().upper()
    try:
        broker = AlpacaBroker()
        price  = broker.get_last_price(symbol)
        bars   = broker.get_bars(symbol, timeframe="1Min", limit=14)
        atr = 0.0
        if len(bars) >= 2:
            trs = []
            for i in range(1, len(bars)):
                h = float(bars[i].get("h", bars[i]["c"]))
                l = float(bars[i].get("l", bars[i]["c"]))
                prev_c = float(bars[i-1]["c"])
                trs.append(max(h-l, abs(h-prev_c), abs(l-prev_c)))
            atr = round(sum(trs)/len(trs), 4) if trs else 0.0
        return jsonify({"symbol": symbol, "price": price, "atr": atr})
    except BrokerError as exc:
        return jsonify({"error": str(exc)}), 503


@app.get("/api/intelligence")
@login_required
def api_intelligence():
    try:
        return jsonify({
            "watchlist": list_watchlist(30),
            "macro":     latest_macro_snapshot(),
            "insiders":  list_insider_activity(10),
            "risk":      list_risk_metrics(20),
            "telemetry": list_performance_events(25),
        })
    except Exception as exc:
        logger.exception("Intelligence fetch failed")
        return jsonify({"error": str(exc)}), 500


@app.get("/api/watchlist")
@login_required
def api_watchlist():
    try:
        return jsonify(list_watchlist(50))
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.get("/api/macro")
@login_required
def api_macro():
    try:
        snap = latest_macro_snapshot()
        return jsonify(snap or {})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.get("/api/insiders")
@login_required
def api_insiders():
    symbol = request.args.get("symbol")
    try:
        return jsonify(list_insider_activity(25, symbol=symbol))
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.get("/api/performance")
@login_required
def api_performance():
    try:
        return jsonify(list_performance_events(100))
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/chart/<symbol>")
@login_required
def chart(symbol: str):
    """Dedicated TradingView chart page for a single symbol."""
    normalized = normalize_symbol(symbol.strip().upper())
    return render_template("chart.html", symbol=normalized, config=config)


@app.get("/api/chart/<symbol>/ohlcv")
@login_required
def api_chart_ohlcv(symbol: str):
    """OHLCV bars + indicator overlays formatted for TradingView Lightweight Charts."""
    normalized = normalize_symbol(symbol.strip().upper())
    timeframe  = request.args.get("timeframe", "1Day")
    try:
        limit = min(max(int(request.args.get("limit", "200")), 2), 1000)
    except (ValueError, TypeError):
        limit = 200

    if timeframe not in _ALLOWED_TIMEFRAMES:
        return jsonify({
            "error": f"Invalid timeframe '{timeframe}'. Allowed: {', '.join(sorted(_ALLOWED_TIMEFRAMES))}",
        }), 400

    # Check cache
    cache_key = f"{normalized}:{timeframe}:{limit}"
    now = time.time()
    cached = _chart_ohlcv_cache.get(cache_key)
    ttl = _CHART_OHLCV_TTL.get(timeframe, _DEFAULT_CHART_TTL)
    if cached and now - cached["ts"] < ttl:
        return jsonify(cached["data"])

    stale_data = False

    broker = get_broker()
    raw_bars = []
    if broker:
        try:
            raw_bars = broker.get_bars(normalized, timeframe=timeframe, limit=limit)
        except Exception as exc:
            logger.warning("Chart OHLCV fetch failed %s %s: %s", normalized, timeframe, exc)

    if not raw_bars:
        raw_bars = _fallback_bars(normalized, count=limit)
        stale_data = True

    # Build TradingView-compatible bar objects {time, open, high, low, close, volume}
    from datetime import date as _date, timedelta, datetime as _dt
    base_date = _date.today() - timedelta(days=len(raw_bars))
    is_daily = timeframe in ("1Day", "1Week", "1Month")
    ohlcv: list[dict] = []
    for i, b in enumerate(raw_bars):
        t_raw = b.get("t")
        if t_raw:
            try:
                dt = _dt.fromisoformat(str(t_raw).replace("Z", "+00:00"))
                time_val: str | int = dt.strftime("%Y-%m-%d") if is_daily else int(dt.timestamp())
            except Exception:
                time_val = (base_date + timedelta(days=i)).isoformat()
        else:
            time_val = (base_date + timedelta(days=i)).isoformat()

        ohlcv.append({
            "time":   time_val,
            "open":   float(b.get("o") or 0),
            "high":   float(b.get("h") or 0),
            "low":    float(b.get("l") or 0),
            "close":  float(b.get("c") or 0),
            "volume": float(b.get("v") or 0),
        })

    closes = [b["close"] for b in ohlcv]
    times  = [b["time"]  for b in ohlcv]

    def _sma(period: int) -> list[dict]:
        result = []
        for i in range(period - 1, len(closes)):
            avg = sum(closes[i - period + 1: i + 1]) / period
            result.append({"time": times[i], "value": round(avg, 4)})
        return result

    def _ema(period: int) -> list[dict]:
        k = 2 / (period + 1)
        result, ema_val = [], None
        for i, c in enumerate(closes):
            ema_val = c if ema_val is None else c * k + ema_val * (1 - k)
            if i >= period - 1:
                result.append({"time": times[i], "value": round(ema_val, 4)})
        return result

    def _rsi(period: int = 14) -> list[dict]:
        if len(closes) < period + 1:
            return []
        gains  = [max(closes[i] - closes[i-1], 0) for i in range(1, len(closes))]
        losses = [max(closes[i-1] - closes[i], 0) for i in range(1, len(closes))]
        result = []
        for i in range(period - 1, len(gains)):
            ag = sum(gains[i - period + 1: i + 1]) / period
            al = sum(losses[i - period + 1: i + 1]) / period
            rsi_val = 100.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag / al)
            result.append({"time": times[i + 1], "value": round(rsi_val, 2)})
        return result

    try:
        indicators_obj = compute_indicators(raw_bars) if raw_bars else None
        indicators_dict = asdict(indicators_obj) if indicators_obj else {}
    except Exception as exc:
        logger.warning("compute_indicators failed for %s: %s", normalized, exc)
        indicators_dict = {}

    payload = {
        "status":     "ok" if not stale_data else "degraded",
        "stale_data": stale_data,
        "symbol":     normalized,
        "timeframe":  timeframe,
        "bars":       ohlcv,
        "indicators": indicators_dict,
        "overlays": {
            "sma20": _sma(20),
            "sma50": _sma(50),
            "ema9":  _ema(9),
            "rsi14": _rsi(14),
        },
        "bar_count": len(ohlcv),
    }

    _chart_ohlcv_cache[cache_key] = {"data": payload, "ts": now}
    return jsonify(payload)


@app.get("/api/chart/<symbol>/signals")
@login_required
def api_chart_signals(symbol: str):
    """Signal events filtered to a single symbol, for the chart page signal history."""
    normalized = normalize_symbol(symbol.strip().upper())
    all_signals = list_signal_events(200)
    filtered = [s for s in all_signals if s.get("symbol") == normalized]
    return jsonify({"status": "ok", "symbol": normalized, "signals": filtered[:50]})


# ══════════════════════════════════════════ STRATEGY PORTFOLIO ROUTES ════════

@app.get("/api/strategies")
@login_required
def api_strategies_list():
    portfolios = list_strategy_portfolios()
    return jsonify({"status": "ok", "strategies": portfolios})


@app.post("/api/strategies")
@login_required
def api_strategies_create():
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()
    if not name:
        return jsonify({"error": "Strategy name is required"}), 400

    allowed_types = {"sma_crossover", "rsi", "vwap"}
    strategy_type = str(data.get("strategy_type", "sma_crossover"))
    if strategy_type not in allowed_types:
        return jsonify({"error": f"strategy_type must be one of: {', '.join(sorted(allowed_types))}"}), 400

    try:
        allocation_pct    = max(0.001, min(1.0, float(data.get("allocation_pct", 0.1))))
        max_position_usd  = max(1.0, float(data.get("max_position_usd", 1000.0)))
        max_drawdown_pct  = max(0.001, min(1.0, float(data.get("max_drawdown_pct", 0.05))))
        daily_loss_limit  = max(1.0, float(data.get("daily_loss_limit_usd", 500.0)))
        min_confidence    = max(0.0, min(100.0, float(data.get("min_confidence", 70.0))))
        min_risk_reward   = max(1.0, float(data.get("min_risk_reward", 5.0)))
    except (TypeError, ValueError) as exc:
        return jsonify({"error": f"Invalid numeric field: {exc}"}), 400

    portfolio_id = create_strategy_portfolio(
        name=name,
        description=str(data.get("description", "")).strip() or None,
        symbols=str(data.get("symbols", "")).strip(),
        strategy_type=strategy_type,
        allocation_pct=allocation_pct,
        max_position_usd=max_position_usd,
        max_drawdown_pct=max_drawdown_pct,
        daily_loss_limit_usd=daily_loss_limit,
        min_confidence=min_confidence,
        min_risk_reward=min_risk_reward,
        autopilot_active=bool(data.get("autopilot_active", False)),
        dry_run=bool(data.get("dry_run", True)),
    )
    logger.info("Strategy portfolio created: '%s' id=%s by %s", name, portfolio_id, session.get("username"))
    return jsonify({"status": "ok", "id": portfolio_id, "name": name}), 201


@app.route("/api/strategies/<int:portfolio_id>", methods=["PATCH"])
@login_required
def api_strategies_update(portfolio_id: int):
    existing = get_strategy_portfolio(portfolio_id)
    if not existing:
        return jsonify({"error": "Strategy not found"}), 404

    data = request.get_json(silent=True) or {}
    updates: dict[str, object] = {}

    if "name" in data:
        name = str(data["name"]).strip()
        if not name:
            return jsonify({"error": "name cannot be empty"}), 400
        updates["name"] = name

    for str_field in ("description", "symbols", "strategy_type"):
        if str_field in data:
            updates[str_field] = str(data[str_field]).strip() or None if str_field == "description" else str(data[str_field]).strip()

    for bool_field in ("autopilot_active", "dry_run"):
        if bool_field in data:
            val = data[bool_field]
            updates[bool_field] = bool(val)

    for float_field in ("allocation_pct", "max_position_usd", "max_drawdown_pct",
                        "daily_loss_limit_usd", "min_confidence", "min_risk_reward"):
        if float_field in data:
            try:
                updates[float_field] = float(data[float_field])
            except (TypeError, ValueError):
                pass

    if "autopilot_active" in updates and updates["autopilot_active"] and not existing.get("dry_run") and config.TRADING_MODE == "live":
        logger.warning("Autopilot armed in LIVE mode for strategy %s by %s", portfolio_id, session.get("username"))

    update_strategy_portfolio(portfolio_id, **updates)
    return jsonify({"status": "ok", "id": portfolio_id})


@app.route("/api/strategies/<int:portfolio_id>", methods=["DELETE"])
@login_required
def api_strategies_delete(portfolio_id: int):
    existing = get_strategy_portfolio(portfolio_id)
    if not existing:
        return jsonify({"error": "Strategy not found"}), 404
    delete_strategy_portfolio(portfolio_id)
    logger.info("Strategy portfolio %s deleted by %s", portfolio_id, session.get("username"))
    return jsonify({"status": "ok"})


# ══════════════════════════════════════════════ SIGNAL EXECUTE ROUTE ══════════

@app.post("/api/execute-signal")
@login_required
def api_execute_signal():
    """Execute a signal as a market, limit, or bracket order."""
    data = request.get_json(silent=True) or {}

    symbol     = normalize_symbol(str(data.get("symbol", "")).strip().upper())
    side       = str(data.get("side", "buy")).lower()
    order_type = str(data.get("order_type", "market")).lower()
    dry_run    = bool(data.get("dry_run", True))
    strategy_portfolio_id = data.get("strategy_portfolio_id")

    try:
        qty = float(data.get("qty", 1))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Invalid qty"}), 400

    if not symbol:
        return jsonify({"ok": False, "error": "Symbol required"}), 400
    if side not in ("buy", "sell"):
        return jsonify({"ok": False, "error": "side must be buy or sell"}), 400
    if qty <= 0:
        return jsonify({"ok": False, "error": "qty must be positive"}), 400
    if order_type not in ("market", "limit", "bracket"):
        return jsonify({"ok": False, "error": "order_type must be market, limit, or bracket"}), 400

    limit_price = data.get("limit_price")
    stop_loss   = data.get("stop_loss")
    take_profit = data.get("take_profit")

    if order_type in ("limit", "bracket") and limit_price is None:
        return jsonify({"ok": False, "error": "limit_price required for limit/bracket orders"}), 400
    if order_type == "bracket" and (stop_loss is None or take_profit is None):
        return jsonify({"ok": False, "error": "stop_loss and take_profit required for bracket orders"}), 400

    try:
        limit_price_f  = float(limit_price) if limit_price is not None else None
        stop_loss_f    = float(stop_loss)   if stop_loss   is not None else None
        take_profit_f  = float(take_profit) if take_profit is not None else None
    except (TypeError, ValueError) as exc:
        return jsonify({"ok": False, "error": f"Invalid price: {exc}"}), 400

    # Validate 5:1 R:R for bracket orders (guard enforcement in the UI)
    if order_type == "bracket" and limit_price_f and stop_loss_f and take_profit_f:
        risk   = abs(limit_price_f - stop_loss_f)
        reward = abs(take_profit_f - limit_price_f)
        rr     = reward / risk if risk > 0 else 0
        if rr < config.MIN_RISK_REWARD_RATIO and not dry_run:
            return jsonify({
                "ok": False,
                "error": f"R:R {rr:.1f}:1 is below minimum {config.MIN_RISK_REWARD_RATIO}:1 guard. Adjust stop/target or use dry-run.",
            }), 400

    try:
        broker = AlpacaBroker()
        price  = broker.get_last_price(symbol)

        if dry_run:
            record_trade_event(
                source="signal_execute", symbol=symbol, side=side, qty=qty,
                order_type=order_type, status="dry_run",
                price=float(limit_price_f or price),
                target_stop_loss=stop_loss_f, target_take_profit=take_profit_f,
                dry_run=True, execution_mode="manual",
            )
            return jsonify({"ok": True, "status": "dry_run", "symbol": symbol, "price": price})

        # Live execution — safety checks already passed above
        if order_type == "market":
            order = broker.place_market_order(symbol, side, qty)
        elif order_type == "limit":
            order = broker.place_limit_order(symbol, side, qty, limit_price_f)
        else:  # bracket
            order = broker.place_bracket_order(symbol, qty, limit_price_f, stop_loss_f, take_profit_f)

        record_trade_event(
            source="signal_execute", symbol=symbol, side=side, qty=qty,
            order_type=order_type, status=order.get("status", "submitted"),
            order_id=order.get("id"), price=price, raw=order,
            target_stop_loss=stop_loss_f, target_take_profit=take_profit_f,
            dry_run=False, execution_mode="manual",
        )

        # Background poll for fill
        order_id_val = order.get("id")
        if order_id_val:
            threading.Thread(target=_poll_order_fill, args=(order_id_val, symbol), daemon=True).start()

        # Update strategy portfolio stats if linked
        if strategy_portfolio_id:
            try:
                sp_id = int(strategy_portfolio_id)
                record_strategy_trade(sp_id, win=False, pnl=0.0)  # placeholder; win/pnl updated on fill
            except Exception:
                pass

        logger.info(
            "execute_signal: %s %s %s qty=%s order_id=%s mode=%s",
            order_type, side, symbol, qty, order_id_val, config.TRADING_MODE,
        )
        return jsonify({"ok": True, "status": order.get("status"), "order_id": order_id_val, "symbol": symbol})

    except (BrokerError, RejectedOrder, ValueError) as exc:
        record_trade_event(
            source="signal_execute", symbol=symbol, side=side, qty=qty,
            order_type=order_type, status="error", error=str(exc), execution_mode="manual",
        )
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        logger.exception("execute_signal failed for %s", symbol)
        return jsonify({"ok": False, "error": str(exc)[:200]}), 500


@app.get("/api/order-preview")
@login_required
def api_order_preview():
    """Return estimated position sizing given symbol + allocation % of portfolio."""
    symbol = normalize_symbol(request.args.get("symbol", "").strip().upper())
    allocation_pct = float(request.args.get("allocation_pct", "0.1") or "0.1")
    if not symbol:
        return jsonify({"error": "symbol required"}), 400
    try:
        broker = AlpacaBroker()
        price  = broker.get_last_price(symbol)
        account = broker.get_account()
        portfolio_value = float(account.get("portfolio_value") or 0)
        buying_power    = float(account.get("buying_power")    or 0)
        alloc_usd  = round(portfolio_value * min(allocation_pct, 1.0), 2)
        max_qty    = alloc_usd / price if price > 0 else 0
        safe_qty   = min(max_qty, buying_power / price) if price > 0 else 0
        return jsonify({
            "symbol":          symbol,
            "price":           price,
            "portfolio_value": portfolio_value,
            "buying_power":    buying_power,
            "alloc_usd":       alloc_usd,
            "suggested_qty":   round(safe_qty, 4),
        })
    except BrokerError as exc:
        return jsonify({"error": str(exc)}), 503


# ══════════════════════════════════════════════ LENS INTELLIGENCE ROUTES ═════

@app.get("/api/lens/summary/<symbol>")
@login_required
def api_lens_summary(symbol: str):
    """AI intelligence summary for a symbol — cached 15 min in memory and Supabase."""
    from trading_agent.ai_research import generate_lens_summary
    normalized = normalize_symbol(symbol.strip().upper())
    if not normalized:
        return jsonify({"error": "symbol required"}), 400

    now = time.time()

    # In-memory cache check
    cached_mem = _lens_summary_cache.get(normalized)
    if cached_mem and now - cached_mem["ts"] < _LENS_SUMMARY_TTL:
        return jsonify({**cached_mem["data"], "cached": True})

    # Supabase cache check
    db_cached = get_cached_lens_report(normalized, "summary")
    if db_cached:
        payload = db_cached.get("content_json", {})
        payload["cached"] = True
        _lens_summary_cache[normalized] = {"data": payload, "ts": now}
        return jsonify(payload)

    # Generate fresh summary
    try:
        broker = AlpacaBroker()
        price = broker.get_last_price(normalized)
        bars  = broker.get_bars(normalized, timeframe="1Day", limit=30)
    except BrokerError:
        price = 0.0
        bars  = []

    try:
        from trading_agent.signal_engine import compute_indicators
        ind = compute_indicators(bars) if bars else None
        indicators_dict = asdict(ind) if ind else {}
    except Exception:
        indicators_dict = {}

    summary = generate_lens_summary(normalized, price, bars, indicators_dict)

    # Persist to memory and Supabase
    _lens_summary_cache[normalized] = {"data": summary, "ts": now}
    try:
        cache_lens_report(
            symbol=normalized,
            report_type="summary",
            content_json=summary,
            conviction_score=summary.get("conviction_score"),
            expires_in_secs=_LENS_SUMMARY_TTL,
        )
    except Exception:
        pass

    return jsonify(summary)


@app.post("/api/lens/ask")
@login_required
def api_lens_ask():
    """Custom AI research query — Ask the Lens anything about a symbol."""
    from trading_agent.ai_research import generate_lens_summary
    data = request.get_json(silent=True) or {}
    symbol = normalize_symbol(str(data.get("symbol", "")).strip().upper())
    query  = str(data.get("query", "")).strip()
    if not symbol or not query:
        return jsonify({"error": "symbol and query are required"}), 400
    if len(query) > 500:
        return jsonify({"error": "query too long (max 500 chars)"}), 400

    try:
        broker = AlpacaBroker()
        price = broker.get_last_price(symbol)
        bars  = broker.get_bars(symbol, timeframe="1Day", limit=20)
    except BrokerError:
        price = 0.0
        bars  = []

    try:
        from trading_agent.signal_engine import compute_indicators
        ind = compute_indicators(bars) if bars else None
        indicators_dict = asdict(ind) if ind else {}
    except Exception:
        indicators_dict = {}

    result = generate_lens_summary(symbol, price, bars, indicators_dict, query=query)

    try:
        cache_lens_report(
            symbol=symbol,
            report_type="ask",
            content_json=result,
            conviction_score=result.get("conviction_score"),
            expires_in_secs=86400,
            query=query,
        )
    except Exception:
        pass

    return jsonify({**result, "symbol": symbol, "query": query})


@app.get("/api/lens/history")
@login_required
def api_lens_history():
    """Recent Lens reports — for the Research page Signal History table."""
    symbol      = request.args.get("symbol", "").strip().upper() or None
    report_type = request.args.get("type", "").strip() or None
    limit       = min(int(request.args.get("limit", 50) or 50), 200)
    reports     = list_lens_reports(limit=limit, symbol=symbol, report_type=report_type)
    return jsonify({"status": "ok", "reports": reports, "count": len(reports)})


# ══════════════════════════════════════════════ BACKTESTING ROUTES ═══════════

@app.get("/backtest")
@login_required
def backtest_page():
    """Render the Backtesting Engine page."""
    recent_runs = list_backtest_runs(20)
    return render_template(
        "backtest.html",
        config=config,
        recent_runs=recent_runs,
        strategies=["sma_crossover", "rsi", "vwap"],
    )


@app.post("/api/backtest")
@login_required
def api_run_backtest():
    """
    Run a historical strategy backtest.
    Simulation-only — never places live or paper orders.
    """
    from trading_agent.backtester import BacktestConfig, NexoBacktester

    data = request.get_json(silent=True) or {}

    raw_symbols = str(data.get("symbols", "")).strip()
    symbols = [normalize_symbol(s.strip()) for s in raw_symbols.split(",") if s.strip()]
    if not symbols:
        return jsonify({"error": "At least one symbol is required"}), 400
    if len(symbols) > 10:
        return jsonify({"error": "Maximum 10 symbols per backtest"}), 400

    strategy = str(data.get("strategy", "sma_crossover"))
    if strategy not in ("sma_crossover", "rsi", "vwap"):
        return jsonify({"error": f"Unknown strategy '{strategy}'"}), 400

    try:
        date_from = str(data.get("date_from", "")).strip() or None
        date_to   = str(data.get("date_to", "")).strip() or None
        initial_capital  = float(data.get("initial_capital", 10000))
        commission_pct   = float(data.get("commission_pct", 0.001))
        slippage_pct     = float(data.get("slippage_pct", 0.0005))
        max_position_pct = float(data.get("max_position_pct", 0.20))
        sma_short        = int(data.get("sma_short", 5))
        sma_long         = int(data.get("sma_long", 20))
        rsi_period       = int(data.get("rsi_period", 14))
        rsi_oversold     = float(data.get("rsi_oversold", 30.0))
        rsi_overbought   = float(data.get("rsi_overbought", 70.0))
    except (TypeError, ValueError) as exc:
        return jsonify({"error": f"Invalid parameter: {exc}"}), 400

    # Guard against unreasonable values
    initial_capital  = max(100.0, min(initial_capital, 1_000_000.0))
    commission_pct   = max(0.0, min(commission_pct, 0.05))
    slippage_pct     = max(0.0, min(slippage_pct, 0.05))
    max_position_pct = max(0.01, min(max_position_pct, 1.0))

    cfg = BacktestConfig(
        symbols=symbols,
        strategy=strategy,
        date_from=date_from or "",
        date_to=date_to or "",
        initial_capital=initial_capital,
        commission_pct=commission_pct,
        slippage_pct=slippage_pct,
        max_position_pct=max_position_pct,
        sma_short=sma_short,
        sma_long=sma_long,
        rsi_period=rsi_period,
        rsi_oversold=rsi_oversold,
        rsi_overbought=rsi_overbought,
    )

    try:
        result = NexoBacktester(cfg).run()
        return jsonify({"ok": True, "result": result.to_dict()})
    except Exception as exc:
        logger.exception("Backtest run failed")
        return jsonify({"ok": False, "error": str(exc)[:300]}), 500


@app.post("/api/optimize")
@login_required
def api_run_optimizer():
    """
    Run strategy parameter optimization (grid search + optional walk-forward).
    Simulation-only — never places live or paper orders.
    """
    from trading_agent.optimizer import OptimizeConfig, StrategyOptimizer

    data = request.get_json(silent=True) or {}

    raw_symbols = str(data.get("symbols", "")).strip()
    symbols = [normalize_symbol(s.strip()) for s in raw_symbols.split(",") if s.strip()]
    if not symbols:
        return jsonify({"error": "At least one symbol is required"}), 400
    if len(symbols) > 5:
        return jsonify({"error": "Maximum 5 symbols for optimization"}), 400

    strategy = str(data.get("strategy", "sma_crossover"))
    if strategy not in ("sma_crossover", "rsi", "vwap"):
        return jsonify({"error": f"Unknown strategy '{strategy}'"}), 400

    try:
        date_from        = str(data.get("date_from", "")).strip() or ""
        date_to          = str(data.get("date_to", "")).strip() or ""
        initial_capital  = float(data.get("initial_capital", 10000))
        walk_forward     = bool(data.get("walk_forward", True))
        rank_by          = str(data.get("rank_by", "sharpe_ratio"))
        max_workers      = min(int(data.get("max_workers", 4)), 8)
    except (TypeError, ValueError) as exc:
        return jsonify({"error": f"Invalid parameter: {exc}"}), 400

    if rank_by not in ("sharpe_ratio", "total_return_pct", "win_rate"):
        rank_by = "sharpe_ratio"

    cfg = OptimizeConfig(
        symbols=symbols,
        strategy=strategy,
        date_from=date_from,
        date_to=date_to,
        initial_capital=max(100.0, min(initial_capital, 1_000_000.0)),
        walk_forward=walk_forward,
        rank_by=rank_by,
        max_workers=max_workers,
        top_n=10,
    )

    try:
        result = StrategyOptimizer(cfg).run()
        return jsonify({"ok": True, "result": result.to_dict()})
    except Exception as exc:
        logger.exception("Optimizer run failed")
        return jsonify({"ok": False, "error": str(exc)[:300]}), 500


@app.get("/api/backtest/runs")
@login_required
def api_backtest_runs():
    """List recent backtest runs from Supabase."""
    limit = min(int(request.args.get("limit", 20) or 20), 100)
    runs = list_backtest_runs(limit)
    return jsonify({"status": "ok", "runs": runs})


@app.cli.command("hash-password")
def hash_password_command():
    password = input("Password to hash: ")
    print(generate_password_hash(password))


if __name__ == "__main__":
    if socketio:
        socketio.run(app, host="127.0.0.1", port=5000, debug=True)
    else:
        app.run(host="127.0.0.1", port=5000, debug=True)
