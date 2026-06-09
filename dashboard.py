import logging
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import datetime, timezone
from functools import wraps
from typing import Callable

from flask import Flask, flash, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from trading_agent import config
from trading_agent.agent import NexoSignalAgent, register_agent_event_listener
from trading_agent.broker import AlpacaBroker, BrokerError
from trading_agent.restrictions import RejectedOrder
from trading_agent.runtime import AgentRuntime
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
_tradingview_quotes: dict[str, dict] = {}
_tradingview_lock = threading.Lock()

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

    # ── US stocks (concurrent: Alpaca snapshot batch + parallel YF 52-wk) ──
    if region == "us":
        try:
            broker = AlpacaBroker()
            # One batch call returns latestTrade, dailyBar, prevDailyBar for all symbols
            snapshots = broker.get_bulk_snapshots_sync(_US_MOST_ACTIVE)

            # Kick off Yahoo Finance calls in parallel for 52-wk range data
            def _yf_fetch(sym):
                return sym, _fetch_yfinance_quote(sym)

            yf_results: dict[str, dict | None] = {}
            with ThreadPoolExecutor(max_workers=10) as pool:
                futures = {pool.submit(_yf_fetch, sym): sym for sym in _US_MOST_ACTIVE}
                for fut in as_completed(futures, timeout=8):
                    try:
                        sym, data = fut.result()
                        yf_results[sym] = data
                    except Exception:
                        pass

            stocks = []
            for sym in _US_MOST_ACTIVE:
                snap = snapshots.get(sym) or snapshots.get(sym.upper())
                if not snap:
                    continue
                try:
                    daily     = snap.get("dailyBar") or {}
                    prev_daily= snap.get("prevDailyBar") or {}
                    cur       = float(daily.get("c") or daily.get("Close") or 0)
                    prev      = float(prev_daily.get("c") or prev_daily.get("Close") or cur)
                    if cur == 0:
                        continue
                    chg     = cur - prev
                    chg_pct = (chg / prev * 100) if prev else 0.0
                    vol     = float(daily.get("v") or daily.get("Volume") or 0)
                    wk_q    = yf_results.get(sym)
                    stocks.append({
                        "symbol":     sym,
                        "name":       sym,
                        "price":      round(cur, 2),
                        "change":     round(chg, 2),
                        "change_pct": round(chg_pct, 2),
                        "volume":     vol,
                        "avg_volume": wk_q["volume"] if wk_q and wk_q.get("volume") else None,
                        "market_cap": None,
                        "pe_ratio":   None,
                        "wk52_high":  wk_q["wk52_high"] if wk_q else None,
                        "wk52_low":   wk_q["wk52_low"]  if wk_q else None,
                        "wk52_chg":   None,
                    })
                except Exception:
                    pass

            # If snapshots returned nothing (outside market hours fallback) try per-bar fetch in parallel
            if not stocks:
                def _bar_fetch(sym):
                    bars = broker.get_bars(sym, timeframe="1Day", limit=2)
                    if not bars:
                        return None
                    cur  = float(bars[-1]["c"])
                    prev = float(bars[-2]["c"]) if len(bars) >= 2 else cur
                    chg  = cur - prev
                    chg_pct = (chg / prev * 100) if prev else 0.0
                    return {
                        "symbol": sym, "name": sym,
                        "price": round(cur, 2), "change": round(chg, 2),
                        "change_pct": round(chg_pct, 2),
                        "volume": float(bars[-1].get("v", 0)),
                        "avg_volume": None, "market_cap": None, "pe_ratio": None,
                        "wk52_high": None, "wk52_low": None, "wk52_chg": None,
                    }
                with ThreadPoolExecutor(max_workers=10) as pool:
                    for row in pool.map(_bar_fetch, _US_MOST_ACTIVE, timeout=15):
                        if row:
                            stocks.append(row)

            # Sort by screen
            if screen == "gainers":
                stocks.sort(key=lambda x: x["change_pct"], reverse=True)
            elif screen == "losers":
                stocks.sort(key=lambda x: x["change_pct"])
            elif screen == "52wk_high":
                stocks.sort(key=lambda x: x["change_pct"], reverse=True)
            elif screen == "52wk_low":
                stocks.sort(key=lambda x: x["change_pct"])
            else:  # most active
                stocks.sort(key=lambda x: x["volume"] or 0, reverse=True)
            result["stocks"] = stocks
        except Exception as e:
            logger.warning("Market data fetch error (US): %s", e)

    # ── Asian indices (parallel Yahoo Finance) ───────────────────────────
    def _fetch_asian(idx_item):
        q = _fetch_yfinance_quote(idx_item["symbol"])
        if not q:
            return None
        return {
            "symbol":     idx_item["symbol"],
            "name":       idx_item["name"],
            "exchange":   idx_item["exchange"],
            "price":      q["price"],
            "change":     q["change"],
            "change_pct": q["change_pct"],
            "volume":     q["volume"],
            "wk52_chg":   None,
        }

    asian = []
    with ThreadPoolExecutor(max_workers=5) as pool:
        for row in pool.map(_fetch_asian, _ASIAN_INDICES, timeout=10):
            if row:
                asian.append(row)
    result["asian"] = asian

    # ── Crypto (parallel Alpaca bars) ────────────────────────────────────
    if region in ("us", "crypto"):
        try:
            broker = AlpacaBroker()

            def _fetch_crypto(item):
                sym = item["symbol"]
                try:
                    bars = broker.get_bars(sym, timeframe="1Day", limit=2)
                    if not bars:
                        return None
                    cur  = float(bars[-1]["c"])
                    prev = float(bars[-2]["c"]) if len(bars) >= 2 else cur
                    chg  = cur - prev
                    chg_pct = (chg / prev * 100) if prev else 0.0
                    return {
                        "symbol":     sym.replace("/USD", ""),
                        "name":       item["name"],
                        "price":      round(cur, 2),
                        "change":     round(chg, 2),
                        "change_pct": round(chg_pct, 2),
                        "volume":     float(bars[-1].get("v", 0)),
                        "market_cap": None,
                        "chg7d":      None,
                    }
                except Exception:
                    return None

            crypto = []
            with ThreadPoolExecutor(max_workers=5) as pool:
                for row in pool.map(_fetch_crypto, _US_CRYPTO, timeout=12):
                    if row:
                        crypto.append(row)
            result["crypto"] = crypto
        except Exception as e:
            logger.warning("Market data fetch error (crypto): %s", e)

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


@app.cli.command("hash-password")
def hash_password_command():
    password = input("Password to hash: ")
    print(generate_password_hash(password))


if __name__ == "__main__":
    if socketio:
        socketio.run(app, host="127.0.0.1", port=5000, debug=True)
    else:
        app.run(host="127.0.0.1", port=5000, debug=True)
