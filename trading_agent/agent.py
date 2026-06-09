"""NexoSignal Agent — core scan loop and order execution.

Scheduled jobs (market-open, scout, macro, insider parse, EOD) live in
``agent_jobs.NexoSignalJobsMixin``.  Scheduler wiring lives in ``scheduler.py``.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import asdict
from typing import Optional

try:
    from apscheduler.schedulers.background import BackgroundScheduler  # noqa: F401
except Exception:  # pragma: no cover
    BackgroundScheduler = None  # type: ignore[assignment,misc]

from . import config
from .agent_jobs import NexoSignalJobsMixin
from .broker import AlpacaBroker, BrokerError, TelegramNotifier
from .events import emit_agent_event, register_agent_event_listener  # re-exported for callers
from .restrictions import (
    RejectedOrder,
    calculate_position_risk,
    check_correlation,
    check_order,
    check_portfolio_var,
    get_tracker,
)
from .signal_engine import analyze_signal
from .storage import (
    record_agent_event,
    record_risk_metrics,
    record_signal_event,
    record_trade_event,
)
from .strategy import (
    BaseStrategy,
    asset_type_for_symbol,
    get_strategy,
)

logger = logging.getLogger("trading_agent")
logging.basicConfig(level=logging.INFO)

# ── Structured JSON logging (optional) ───────────────────────────────────────

class _JsonFormatter(logging.Formatter):
    """Emit one JSON object per log line to a structured log file."""

    def format(self, record: logging.LogRecord) -> str:
        import json
        doc: dict = {
            "ts": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            doc["exc"] = self.formatException(record.exc_info)
        for key in ("symbol", "event_type"):
            if hasattr(record, key):
                doc[key] = getattr(record, key)
        return json.dumps(doc, ensure_ascii=False)


def _setup_structured_logging() -> None:
    """Attach a JSON file handler when STRUCTURED_LOG_FILE is set in env."""
    path = getattr(config, "STRUCTURED_LOG_FILE", "")
    if not path:
        return
    fh = logging.FileHandler(path, encoding="utf-8")
    fh.setFormatter(_JsonFormatter())
    logging.getLogger("trading_agent").addHandler(fh)


_setup_structured_logging()


# ── Core Agent ────────────────────────────────────────────────────────────────

class NexoSignalAgent(NexoSignalJobsMixin):
    """Autonomous trading agent: scan loop + AlphaCore execution.

    Scheduled jobs are provided by ``NexoSignalJobsMixin`` in ``agent_jobs.py``.
    """

    def __init__(
        self,
        symbols: list[str],
        strategy_name: str = "sma_crossover",
        qty_per_trade: float = 1,
        poll_interval_sec: int = 60,
        dry_run: bool = False,
    ):
        self.broker = AlpacaBroker()
        self.symbols = [s.upper() for s in symbols]
        self.strategy: BaseStrategy = get_strategy(strategy_name)
        self.qty_per_trade = qty_per_trade
        self.poll_interval = poll_interval_sec
        self.dry_run = dry_run
        self._running = False
        self.log: list[dict] = []
        self.errors: list[str] = []
        self.notifier = TelegramNotifier()
        self.strike_count = 0
        self.circuit_breaker_active = False
        self.autonomous_orders: dict[str, dict] = {}
        self.session_trade_attempts = 0
        self.trade_floor_logged = False
        self.started_at = time.time()
        self._stop_event = threading.Event()
        # Per-symbol error isolation (Phase C)
        self._symbol_error_counts: dict[str, int] = {}
        self._quarantined: set[str] = set()

    def _log(
        self,
        msg: str,
        level: str = "info",
        event_type: str = "agent_log",
        symbol: str | None = None,
        payload: dict | None = None,
    ) -> None:
        ts = __import__("datetime").datetime.now().strftime("%H:%M:%S")
        self.log.append({"time": ts, "msg": msg, "level": level})
        getattr(logger, level)(msg)
        try:
            record_agent_event(
                layer="NexoSignal Agent",
                event_type=event_type,
                message=msg,
                severity=level,
                symbol=symbol,
                payload=payload,
            )
        except Exception:
            logger.exception("NexoSignal Ledger log write failed")

    # ── Symbol evaluation (scan loop core) ───────────────────────────────────

    def evaluate_symbol(self, symbol: str) -> Optional[str]:
        """Fetch bars, run strategy, return order id or None."""
        if self.circuit_breaker_active:
            self._log(
                f"{symbol}: scan blocked by active circuit breaker",
                "warning",
                "execution_blocked",
                symbol,
            )
            return None
        started = time.perf_counter()
        bars = self.broker.get_bars(symbol, timeframe="1Min", limit=60)
        if not bars:
            self._log(f"{symbol}: no bar data available", "warning")
            return None

        base_signal = self.strategy.signal(bars)
        price = float(bars[-1]["c"])
        decision = analyze_signal(symbol, bars, base_signal, config.SIGNAL_MIN_CONFIDENCE)
        signal = decision.final_signal
        record_signal_event(
            symbol=symbol,
            strategy=self.strategy.name,
            base_signal=decision.base_signal,
            final_signal=decision.final_signal,
            confidence=decision.confidence,
            approved=decision.approved,
            reason=decision.reason,
            price=price,
            indicators=asdict(decision.indicators),
            confluence_score=decision.confidence,
            order_book_imbalance=0.0,
        )
        self._log(
            f"{symbol} @ ${price:.2f} -> base={base_signal.upper()} "
            f"final={signal.upper()} confidence={decision.confidence:.0f}"
        )
        self._record_telemetry(
            stage="symbol_evaluation",
            symbol=symbol,
            latency_ms=(time.perf_counter() - started) * 1000,
            payload={
                "base_signal": base_signal,
                "final_signal": signal,
                "confidence": decision.confidence,
                "approved": decision.approved,
            },
        )

        if signal == "hold":
            if base_signal != "hold":
                self._log(f"{symbol}: signal blocked - {decision.reason}", "warning")
            return None

        position = self.broker.get_position(symbol)
        if signal == "buy" and position:
            self._log(f"{symbol}: already holding {position['qty']} shares, skipping buy")
            return None
        if signal == "sell" and not position:
            self._log(f"{symbol}: no position to sell, skipping")
            return None

        if self.dry_run:
            try:
                acc = self.broker.get_account()
                check_order(
                    symbol=symbol,
                    side=signal,
                    qty=self.qty_per_trade,
                    price=price,
                    portfolio_value=float(acc.get("portfolio_value", 0)),
                    available_cash=float(acc.get("cash", 0)),
                )
            except RejectedOrder as exc:
                self._log(
                    f"[DRY RUN] Guard would REJECT {signal.upper()} {symbol}: {exc}",
                    "warning",
                    "guard_dry_run_reject",
                    symbol,
                )
                return None
            self._log(f"[DRY RUN] Would place {signal.upper()} {self.qty_per_trade} {symbol}")
            record_trade_event(
                source="bot",
                symbol=symbol,
                side=signal,
                qty=self.qty_per_trade,
                order_type="market",
                status="dry_run",
                price=price,
                strategy=self.strategy.name,
                dry_run=True,
                execution_mode="autonomous_agent",
            )
            return None

        try:
            order = self.broker.place_market_order(symbol, signal, self.qty_per_trade)
            order_id = order.get("id", "?")
            self._log(f"Order placed: {signal.upper()} {self.qty_per_trade} {symbol} - id={order_id}")
            record_trade_event(
                source="bot",
                symbol=symbol,
                side=signal,
                qty=self.qty_per_trade,
                order_type="market",
                status=order.get("status", "submitted"),
                order_id=order_id,
                price=price,
                strategy=self.strategy.name,
                dry_run=False,
                raw=order,
                execution_mode="autonomous_agent",
            )
            return order_id
        except RejectedOrder as e:
            self._log(f"Order REJECTED for {symbol}: {e}", "warning")
            record_trade_event(
                source="bot",
                symbol=symbol,
                side=signal,
                qty=self.qty_per_trade,
                order_type="market",
                status="rejected",
                price=price,
                strategy=self.strategy.name,
                dry_run=False,
                error=str(e),
                execution_mode="autonomous_agent",
            )
        except BrokerError as e:
            self._log(f"Broker error for {symbol}: {e}", "error")
            self.errors.append(str(e))
            record_trade_event(
                source="bot",
                symbol=symbol,
                side=signal,
                qty=self.qty_per_trade,
                order_type="market",
                status="error",
                price=price,
                strategy=self.strategy.name,
                dry_run=False,
                error=str(e),
                execution_mode="autonomous_agent",
            )
        return None

    # ── AlphaCore bracket execution ───────────────────────────────────────────

    def execute_alpha_pick(self, pick, allocation_weight: float | None = None) -> None:
        """NexoSignal Executor bracket-order flow."""
        started = time.perf_counter()
        asset_type = asset_type_for_symbol(pick.symbol)
        if self.circuit_breaker_active:
            self._log(
                f"NexoSignal Guard blocked {pick.symbol}: circuit breaker active",
                "warning",
                "execution_blocked",
                pick.symbol,
            )
            return
        if asset_type == "crypto":
            self._log(
                f"NexoSignal Guard blocked {pick.symbol}: crypto bracket execution is not enabled",
                "warning",
                "execution_blocked",
                pick.symbol,
                {"asset_type": asset_type},
            )
            return
        account = self.broker.get_account()
        settled_cash = float(account.get("cash", 0))
        portfolio_value = float(account.get("portfolio_value", 0) or 0)
        target_weight = allocation_weight if allocation_weight is not None else config.POSITION_SIZE_PCT
        capital_allocated = min(settled_cash * config.POSITION_SIZE_PCT, portfolio_value * target_weight)
        qty = math.floor(capital_allocated / max(pick.price, 0.01))
        active_symbols = [str(p.get("symbol", "")).upper() for p in self.broker.get_positions()]
        try:
            check_correlation(pick.symbol, active_symbols)
        except RejectedOrder as exc:
            original_qty = qty
            qty = math.floor(qty * 0.5)
            capital_allocated = qty * pick.price
            self._log(
                f"NexoSignal Guard scaled {pick.symbol} 50% for correlation: {exc}",
                "warning",
                "correlation_scaled",
                pick.symbol,
                {"original_qty": original_qty, "scaled_qty": qty, "reason": str(exc)},
            )
        if qty <= 0:
            self._log(
                f"NexoSignal Guard blocked {pick.symbol}: insufficient settled cash",
                "warning",
                "execution_blocked",
                pick.symbol,
            )
            return
        stop_loss = pick.price - pick.atr
        take_profit = pick.price + (5 * pick.atr)
        rr = ((take_profit - pick.price) / max(pick.price - stop_loss, 0.0001)) if stop_loss < pick.price else 0
        var_1d, var_1d_pct = calculate_position_risk(pick.price, qty, pick.atr)
        try:
            proposed_positions = [{"var_1d": var_1d}]
            check_portfolio_var(proposed_positions, portfolio_value)
            record_risk_metrics(
                symbol=pick.symbol,
                var_1d=var_1d,
                var_1d_pct=var_1d_pct,
                portfolio_var=var_1d / max(portfolio_value, 0.0001),
                correlation_flag=False,
            )
        except RejectedOrder as exc:
            self._log(
                f"NexoSignal Guard blocked {pick.symbol}: {exc}",
                "warning",
                "execution_blocked",
                pick.symbol,
            )
            return

        if self.dry_run:
            self.session_trade_attempts += 1
            record_signal_event(
                symbol=pick.symbol,
                strategy="NexoSignal AlphaCore",
                base_signal="buy",
                final_signal="buy",
                confidence=pick.confluence_score,
                approved=True,
                reason=f"DRY RUN AlphaCore probability={pick.probability:.2f}",
                price=pick.price,
                indicators=asdict(pick) if hasattr(pick, "__dataclass_fields__") else dict(pick),
                confluence_score=pick.confluence_score,
                order_book_imbalance=pick.order_book_imbalance,
            )
            record_trade_event(
                source="bot",
                symbol=pick.symbol,
                side="buy",
                qty=qty,
                order_type="bracket",
                status="dry_run",
                price=pick.price,
                strategy="NexoSignal AlphaCore",
                dry_run=True,
                raw={"asset_type": asset_type, "capital_allocated": capital_allocated, "allocation_weight": target_weight},
                target_stop_loss=stop_loss,
                target_take_profit=take_profit,
                current_risk_reward_ratio=rr,
                execution_mode="autonomous_agent",
            )
            self._log(
                f"[DRY RUN] NexoSignal Executor would submit bracket order for {pick.symbol}",
                event_type="dry_run_order",
                symbol=pick.symbol,
                payload={"qty": qty, "capital_allocated": capital_allocated, "stop_loss": stop_loss, "take_profit": take_profit},
            )
            self._record_telemetry(
                stage="dry_run_order",
                symbol=pick.symbol,
                latency_ms=(time.perf_counter() - started) * 1000,
                slippage_bps=getattr(pick, "slippage_estimate_bps", None),
                payload={"qty": qty, "capital_allocated": capital_allocated, "confluence_score": pick.confluence_score},
            )
            return

        try:
            order = self.broker.place_bracket_order(pick.symbol, qty, pick.price, stop_loss, take_profit)
            self.session_trade_attempts += 1
            legs = order.get("legs", [])
            stop_order_id = next(
                (leg.get("id") for leg in legs if leg.get("type") in ("stop", "stop_limit")),
                None,
            )
            self.autonomous_orders[pick.symbol] = {
                "entry_price": pick.price,
                "qty": qty,
                "atr": pick.atr,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "break_even_moved": False,
                "order_id": order.get("id"),
                "stop_order_id": stop_order_id,
            }
            record_signal_event(
                symbol=pick.symbol,
                strategy="NexoSignal AlphaCore",
                base_signal="buy",
                final_signal="buy",
                confidence=pick.confluence_score,
                approved=True,
                reason=f"AlphaCore probability={pick.probability:.2f}",
                price=pick.price,
                indicators=asdict(pick) if hasattr(pick, "__dataclass_fields__") else dict(pick),
                confluence_score=pick.confluence_score,
                order_book_imbalance=pick.order_book_imbalance,
            )
            record_trade_event(
                source="bot",
                symbol=pick.symbol,
                side="buy",
                qty=qty,
                order_type="bracket",
                status=order.get("status", "submitted"),
                order_id=order.get("id"),
                price=pick.price,
                strategy="NexoSignal AlphaCore",
                dry_run=False,
                raw=order,
                target_stop_loss=stop_loss,
                target_take_profit=take_profit,
                current_risk_reward_ratio=rr,
                execution_mode="autonomous_agent",
            )
            self._log(
                f"NexoSignal Executor submitted bracket order for {pick.symbol}",
                event_type="order_submitted",
                symbol=pick.symbol,
                payload={"qty": qty, "stop_loss": stop_loss, "take_profit": take_profit},
            )
            self._record_telemetry(
                stage="order_submitted",
                symbol=pick.symbol,
                latency_ms=(time.perf_counter() - started) * 1000,
                slippage_bps=getattr(pick, "slippage_estimate_bps", None),
                payload={
                    "qty": qty,
                    "capital_allocated": capital_allocated,
                    "asset_type": asset_type,
                    "probability": pick.probability,
                    "confluence_score": pick.confluence_score,
                    "liquidity_score": getattr(pick, "liquidity_score", None),
                },
            )
            self._send_trade_alert_async(
                symbol=pick.symbol,
                asset_type=asset_type,
                qty=qty,
                capital_allocated=capital_allocated,
                confluence_score=pick.confluence_score,
                entry=pick.price,
                stop=stop_loss,
                target=take_profit,
            )
        except (BrokerError, RejectedOrder) as exc:
            self._log(
                f"NexoSignal Executor failed {pick.symbol}: {exc}",
                "error",
                "order_error",
                pick.symbol,
            )

    def _send_trade_alert_async(
        self,
        *,
        symbol: str,
        asset_type: str,
        qty: float,
        capital_allocated: float,
        confluence_score: float,
        entry: float,
        stop: float,
        target: float,
    ) -> None:
        def _send() -> None:
            try:
                self.notifier.trade_opened_detailed(
                    symbol=symbol,
                    asset_type=asset_type,
                    qty=qty,
                    capital_allocated=capital_allocated,
                    confluence_score=confluence_score,
                    entry=entry,
                    stop=stop,
                    target=target,
                )
            except Exception:
                logger.exception("NexoSignal Telegram alert failed")

        threading.Thread(target=_send, daemon=True).start()

    # ── Scan loop ─────────────────────────────────────────────────────────────

    def run_once(self) -> None:
        """Single scan across all watched symbols with per-symbol error isolation."""
        market_open = self.broker.is_market_open()
        tracker = get_tracker()
        self._log(
            f"Scan - strategy={self.strategy.name} | "
            f"trades today={tracker.trades_today}/{config.MAX_TRADES_PER_DAY}"
        )

        for symbol in self.symbols:
            if symbol in self._quarantined:
                self._log(
                    f"{symbol}: skipped (quarantined after repeated errors)",
                    "warning",
                    "symbol_quarantined",
                    symbol,
                )
                continue
            # Crypto trades 24/7; equities require market hours
            if asset_type_for_symbol(symbol) != "crypto" and not market_open:
                continue
            try:
                self.evaluate_symbol(symbol)
                self._symbol_error_counts[symbol] = 0  # reset on success
            except Exception as exc:
                count = self._symbol_error_counts.get(symbol, 0) + 1
                self._symbol_error_counts[symbol] = count
                logger.exception("Symbol scan error: %s (consecutive=%d)", symbol, count)
                self._log(
                    f"{symbol}: scan error {exc} (consecutive #{count})",
                    "error",
                    "symbol_scan_error",
                    symbol,
                    {"consecutive_errors": count},
                )
                self.errors.append(str(exc))
                if count >= 3:
                    self._quarantined.add(symbol)
                    self._log(
                        f"{symbol}: quarantined after {count} consecutive errors — skipped until agent restart",
                        "warning",
                        "symbol_quarantined",
                        symbol,
                        {"quarantined": True},
                    )

    def run(self) -> None:
        """Block and run the agent loop indefinitely."""
        self._running = True
        self._stop_event.clear()
        self._log(
            f"Agent started. Mode={'DRY RUN' if self.dry_run else 'LIVE'} "
            f"Strategy={self.strategy.name} Symbols={self.symbols} "
            f"Interval={self.poll_interval}s"
        )
        try:
            while self._running:
                try:
                    self.run_once()
                except BrokerError as e:
                    self._log(f"Broker error stopped bot: {e}", "error")
                    self.errors.append(str(e))
                    self._running = False
                    break
                self._log(f"Sleeping {self.poll_interval}s...")
                self._stop_event.wait(timeout=self.poll_interval)
        except KeyboardInterrupt:
            self._log("Agent stopped by user.")
            self._running = False
        finally:
            self._running = False

    def stop(self) -> None:
        self._running = False
        self._stop_event.set()
        self._log("Stop requested.")

    def status(self) -> dict:
        try:
            acc = self.broker.get_account()
            clock = self.broker.get_clock()
            positions = self.broker.get_positions()
            tracker = get_tracker()
            return {
                "mode": "paper" if "paper" in self.broker._base else "live",
                "dry_run": self.dry_run,
                "market_open": clock["is_open"],
                "portfolio_value": float(acc["portfolio_value"]),
                "cash": float(acc["cash"]),
                "buying_power": float(acc["buying_power"]),
                "positions": [
                    {
                        "symbol": p["symbol"],
                        "qty": float(p["qty"]),
                        "market_value": float(p["market_value"]),
                        "unrealized_pl": float(p["unrealized_pl"]),
                    }
                    for p in positions
                ],
                "trades_today": tracker.trades_today,
                "loss_today": tracker.loss_today,
                "strategy": self.strategy.name,
                "watching": self.symbols,
                "strike_count": self.strike_count,
                "circuit_breaker_active": self.circuit_breaker_active,
                "quarantined_symbols": sorted(self._quarantined),
            }
        except BrokerError as e:
            return {"error": str(e)}


# Backward-compat alias
TradingAgent = NexoSignalAgent

# Re-export scheduler function so existing callers (runtime.py) keep working
from .scheduler import start_nexosignal_scheduler  # noqa: E402


if __name__ == "__main__":
    agent = NexoSignalAgent(config.DEFAULT_DASHBOARD_SYMBOLS, dry_run=True)
    from .runtime import AgentRuntime

    runtime = AgentRuntime(agent)
    runtime.start()
    try:
        while True:
            time.sleep(5)
    except KeyboardInterrupt:
        runtime.stop()
