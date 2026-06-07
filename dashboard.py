import logging
import threading
from dataclasses import asdict
from functools import wraps
from typing import Callable

from flask import Flask, flash, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from trading_agent import config
from trading_agent.agent import TradingAgent, register_agent_event_listener
from trading_agent.broker import AlpacaBroker, BrokerError
from trading_agent.restrictions import RejectedOrder
from trading_agent.storage import (
    init_db,
    latest_wallet_connection,
    list_brokerage_connections,
    list_signal_events,
    list_trade_events,
    record_brokerage_connection,
    record_trade_event,
    record_wallet_connection,
    signal_summary,
    trade_summary,
)
from trading_agent.strategy import STRATEGIES

logger = logging.getLogger("dashboard")

try:
    from flask_socketio import SocketIO
except Exception:  # pragma: no cover - dependency may not be installed yet
    SocketIO = None


app = Flask(__name__)
app.secret_key = config.FLASK_SECRET_KEY
socketio = SocketIO(app, cors_allowed_origins="*") if SocketIO else None
init_db()

_agent: TradingAgent | None = None
_agent_thread: threading.Thread | None = None
_agent_lock = threading.Lock()


def publish_realtime_event(event_type: str, payload: dict) -> None:
    if socketio:
        socketio.emit(event_type, payload)


register_agent_event_listener(publish_realtime_event)


@app.context_processor
def inject_app_config():
    return {"config": config}


def password_matches(raw_password: str) -> bool:
    expected = config.DASHBOARD_PASSWORD
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
        broker = AlpacaBroker()
        broker.get_account()
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
        agent = _agent
        thread = _agent_thread
    return {
        "running": bool(agent and agent._running and thread and thread.is_alive()),
        "configured": agent is not None,
        "symbols": agent.symbols if agent else [],
        "strategy": agent.strategy.name if agent else None,
        "qty": agent.qty_per_trade if agent else None,
        "interval": agent.poll_interval if agent else None,
        "dry_run": agent.dry_run if agent else None,
        "log": agent.log[-25:] if agent else [],
        "errors": agent.errors[-10:] if agent else [],
    }


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if config.DASHBOARD_PASSWORD == "change-me":
            flash("Set DASHBOARD_PASSWORD in .env before using the dashboard.", "error")
            return render_template("login.html")
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if username == config.DASHBOARD_USERNAME and password_matches(password):
            session["logged_in"] = True
            return redirect(request.args.get("next") or url_for("dashboard"))
        flash("Invalid username or password.", "error")
    return render_template("login.html")


@app.post("/logout")
@login_required
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def dashboard():
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

    return render_template(
        "dashboard.html",
        account=account,
        clock=clock,
        positions=positions,
        open_orders=open_orders,
        connection_error=connection_error,
        agent=current_agent_status(),
        trades=list_trade_events(100),
        summary=trade_summary(),
        wallet=latest_wallet_connection(),
        brokerages=list_brokerage_connections(),
        signals=list_signal_events(100),
        signal_summary=signal_summary(),
        config=config,
        strategies=sorted(STRATEGIES.keys()),
    )


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
    provider = request.form.get("provider", "").strip().lower()
    account_label = request.form.get("account_label", "").strip()
    account_id = request.form.get("account_id", "").strip() or None
    environment = request.form.get("environment", "paper").strip().lower()
    status = request.form.get("status", "manual").strip().lower()
    api_key_last4 = request.form.get("api_key_last4", "").strip() or None
    notes = request.form.get("notes", "").strip() or None

    if not provider or not account_label:
        flash("Provider and account label are required.", "error")
        return redirect(url_for("dashboard"))

    record_brokerage_connection(
        provider=provider,
        account_label=account_label,
        account_id=account_id,
        environment=environment,
        status=status,
        api_key_last4=api_key_last4,
        notes=notes,
    )
    flash("Brokerage connection saved.", "success")
    return redirect(url_for("dashboard"))


@app.post("/manual-order")
@login_required
def manual_order():
    symbol = request.form.get("symbol", "").strip().upper()
    side = request.form.get("side", "").strip().lower()
    qty = float(request.form.get("qty", "0"))
    dry_run = request.form.get("dry_run") == "on"

    if not symbol or side not in {"buy", "sell"} or qty <= 0:
        flash("Enter a valid symbol, side, and quantity.", "error")
        return redirect(url_for("dashboard"))

    try:
        broker = AlpacaBroker()
        price = broker.get_last_price(symbol)
        if dry_run:
            record_trade_event(
                source="manual",
                symbol=symbol,
                side=side,
                qty=qty,
                order_type="market",
                status="dry_run",
                price=price,
                dry_run=True,
                execution_mode="manual",
            )
            flash(f"Dry run recorded: {side.upper()} {qty} {symbol}.", "success")
            return redirect(url_for("dashboard"))

        order = broker.place_market_order(symbol, side, qty)
        record_trade_event(
            source="manual",
            symbol=symbol,
            side=side,
            qty=qty,
            order_type="market",
            status=order.get("status", "submitted"),
            order_id=order.get("id"),
            price=price,
            raw=order,
            execution_mode="manual",
        )
        flash(f"Order submitted: {side.upper()} {qty} {symbol}.", "success")
    except (BrokerError, RejectedOrder, ValueError) as e:
        record_trade_event(
            source="manual",
            symbol=symbol or "?",
            side=side or "?",
            qty=qty if qty > 0 else 0,
            order_type="market",
            status="error",
            error=str(e),
            execution_mode="manual",
        )
        flash(f"Order failed: {e}", "error")

    return redirect(url_for("dashboard"))


@app.post("/bot/start")
@login_required
def start_bot():
    global _agent, _agent_thread
    if config.RUNNING_ON_VERCEL:
        flash("Bot loops cannot run on Vercel serverless. Run the bot locally or on a VPS; use Vercel for the dashboard.", "error")
        return redirect(url_for("dashboard"))

    symbols = [
        s.strip().upper()
        for s in request.form.get("symbols", "").split(",")
        if s.strip()
    ] or config.DEFAULT_DASHBOARD_SYMBOLS
    strategy = request.form.get("strategy", "sma_crossover")
    qty = float(request.form.get("qty", "1"))
    interval = int(request.form.get("interval", "60"))
    dry_run = request.form.get("dry_run") == "on"

    with _agent_lock:
        if _agent and _agent._running and _agent_thread and _agent_thread.is_alive():
            flash("Bot is already running.", "error")
            return redirect(url_for("dashboard"))

        connection_error = validate_alpaca_connection()
        if connection_error:
            flash(f"Could not start bot: {friendly_broker_error(connection_error)}", "error")
            return redirect(url_for("dashboard"))

        try:
            _agent = TradingAgent(
                symbols=symbols,
                strategy_name=strategy,
                qty_per_trade=qty,
                poll_interval_sec=interval,
                dry_run=dry_run,
            )
        except (BrokerError, ValueError) as e:
            flash(f"Could not start bot: {e}", "error")
            return redirect(url_for("dashboard"))

        _agent_thread = threading.Thread(target=_agent.run, daemon=True)
        _agent_thread.start()

    flash("Bot started.", "success")
    return redirect(url_for("dashboard"))


@app.post("/bot/stop")
@login_required
def stop_bot():
    with _agent_lock:
        if _agent:
            _agent.stop()
    flash("Bot stop requested.", "success")
    return redirect(url_for("dashboard"))


@app.post("/orders/<order_id>/cancel")
@login_required
def cancel_order(order_id: str):
    try:
        broker = AlpacaBroker()
        broker.cancel_order(order_id)
        flash(f"Cancelled order {order_id}.", "success")
    except BrokerError as e:
        flash(f"Cancel failed: {e}", "error")
    return redirect(url_for("dashboard"))


# ─── JSON API endpoints (used by Chart.js and AI research panel) ───────────

@app.get("/api/chart-data")
@login_required
def api_chart_data():
    """Chart.js data: signal confidence trend, approval stats, P&L proxy."""
    trades = list_trade_events(500)
    signals = list_signal_events(200)

    # Confidence distribution histogram (10 buckets of 10)
    conf_buckets = [0] * 10
    for s in signals:
        bucket = min(int(float(s.get("confidence", 0)) / 10), 9)
        conf_buckets[bucket] += 1

    approved_count = sum(1 for s in signals if s.get("approved"))
    rejected_count = len(signals) - approved_count

    # Recent signal trend (last 30, oldest first)
    recent = list(reversed(signals[:30]))
    signal_trend = [
        {
            "label": f"{s.get('symbol','')} {s.get('created_at','')[:10]}",
            "confidence": round(float(s.get("confidence", 0)), 1),
            "signal": s.get("final_signal", "hold"),
            "approved": bool(s.get("approved")),
        }
        for s in recent
    ]

    # Buy/sell/hold distribution
    buy_n = sum(1 for s in signals if s.get("final_signal") == "buy")
    sell_n = sum(1 for s in signals if s.get("final_signal") == "sell")
    hold_n = sum(1 for s in signals if s.get("final_signal") == "hold")

    # Trade count by strategy
    strategy_counts: dict[str, int] = {}
    for t in trades:
        strat = t.get("strategy") or "manual"
        strategy_counts[strat] = strategy_counts.get(strat, 0) + 1

    return jsonify({
        "confidence_distribution": {
            "labels": ["0-10", "10-20", "20-30", "30-40", "40-50",
                       "50-60", "60-70", "70-80", "80-90", "90-100"],
            "data": conf_buckets,
        },
        "approval_stats": {"approved": approved_count, "rejected": rejected_count},
        "signal_direction": {"buy": buy_n, "sell": sell_n, "hold": hold_n},
        "signal_trend": signal_trend,
        "strategy_counts": strategy_counts,
    })


@app.get("/api/live-metrics")
@login_required
def api_live_metrics():
    """Live portfolio snapshot for dashboard auto-refresh."""
    broker = get_broker()
    if not broker:
        return jsonify({"error": "Broker unavailable"}), 503
    try:
        account = broker.get_account()
        positions = broker.get_positions()
        clock = broker.get_clock()
        total_unrealized = sum(float(p.get("unrealized_pl", 0)) for p in positions)
        return jsonify({
            "portfolio_value": float(account.get("portfolio_value", 0)),
            "cash": float(account.get("cash", 0)),
            "buying_power": float(account.get("buying_power", 0)),
            "market_open": bool(clock.get("is_open", False)),
            "total_unrealized_pl": round(total_unrealized, 2),
            "positions": [
                {
                    "symbol": p["symbol"],
                    "qty": float(p["qty"]),
                    "market_value": float(p["market_value"]),
                    "unrealized_pl": float(p["unrealized_pl"]),
                    "unrealized_plpc": round(float(p.get("unrealized_plpc", 0)) * 100, 2),
                }
                for p in positions
            ],
        })
    except BrokerError as exc:
        return jsonify({"error": str(exc)}), 503


@app.get("/api/ai-research/<symbol>")
@login_required
def api_ai_research(symbol: str):
    """
    Run multi-agent AI research on a symbol.
    Gemini (market analysis) + Grok (news) run in parallel,
    then Claude validates the 5:1 risk-reward trade decision.
    """
    symbol = symbol.strip().upper()
    if not symbol:
        return jsonify({"error": "Symbol required"}), 400

    try:
        broker = AlpacaBroker()
        bars = broker.get_bars(symbol, timeframe="1Min", limit=30)
        if not bars:
            return jsonify({"error": f"No bar data available for {symbol}"}), 404

        price = float(bars[-1]["c"])

        from trading_agent.signal_engine import analyze_signal
        decision = analyze_signal(symbol, bars, "hold", config.SIGNAL_MIN_CONFIDENCE)
        indicators_dict = asdict(decision.indicators)

        from trading_agent.ai_research import run_multi_agent_research
        research = run_multi_agent_research(
            symbol=symbol,
            price=price,
            bars=bars,
            base_signal=decision.base_signal,
            base_confidence=decision.confidence,
            indicators=indicators_dict,
        )

        def _sig(s):
            if not s:
                return None
            return {
                "provider": s.provider,
                "signal": s.signal,
                "confidence": s.confidence,
                "rationale": s.rationale,
                "price_target": s.price_target,
                "stop_loss": s.stop_loss,
                "risk_reward_ratio": s.risk_reward_ratio,
                "sentiment": s.sentiment,
                "timeframe": s.timeframe,
                "news_catalyst": s.news_catalyst,
                "error": s.error,
            }

        return jsonify({
            "symbol": research.symbol,
            "current_price": price,
            "gemini": _sig(research.gemini),
            "grok": _sig(research.grok),
            "claude": _sig(research.claude),
            "consensus_signal": research.consensus_signal,
            "consensus_confidence": research.consensus_confidence,
            "approved_5to1": research.approved_5to1,
            "entry_price": research.entry_price,
            "stop_loss": research.stop_loss,
            "take_profit": research.take_profit,
            "risk_reward_ratio": research.risk_reward_ratio,
            "min_ratio_required": config.AI_MIN_RISK_REWARD_RATIO,
            "technical": {
                "base_signal": decision.base_signal,
                "final_signal": decision.final_signal,
                "confidence": decision.confidence,
                "approved": decision.approved,
                "reason": decision.reason,
            },
        })

    except BrokerError as exc:
        return jsonify({"error": f"Broker error: {exc}"}), 503
    except Exception as exc:
        logger.exception("AI research failed for %s", symbol)
        return jsonify({"error": str(exc)}), 500


@app.get("/api/price/<symbol>")
@login_required
def api_price(symbol: str):
    """Quick price + bar snapshot for the risk calculator."""
    symbol = symbol.strip().upper()
    try:
        broker = AlpacaBroker()
        price = broker.get_last_price(symbol)
        bars = broker.get_bars(symbol, timeframe="1Min", limit=14)
        # Compute ATR
        atr = 0.0
        if len(bars) >= 2:
            trs = []
            for i in range(1, len(bars)):
                h = float(bars[i].get("h", bars[i]["c"]))
                l = float(bars[i].get("l", bars[i]["c"]))
                prev_c = float(bars[i - 1]["c"])
                trs.append(max(h - l, abs(h - prev_c), abs(l - prev_c)))
            atr = round(sum(trs) / len(trs), 4) if trs else 0.0
        return jsonify({"symbol": symbol, "price": price, "atr": atr})
    except BrokerError as exc:
        return jsonify({"error": str(exc)}), 503


@app.cli.command("hash-password")
def hash_password_command():
    password = input("Password to hash: ")
    print(generate_password_hash(password))


if __name__ == "__main__":
    if socketio:
        socketio.run(app, host="127.0.0.1", port=5000, debug=True)
    else:
        app.run(host="127.0.0.1", port=5000, debug=True)
