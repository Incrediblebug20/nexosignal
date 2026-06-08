"""
Trading agent loop. Runs on a configurable interval, evaluates the strategy
for each watched symbol, and places orders when signalled.
"""

import logging
import math
import asyncio
import time
from dataclasses import asdict
from datetime import datetime, time as clock_time
from typing import Optional

import pytz

try:
    from apscheduler.schedulers.background import BackgroundScheduler
except Exception:  # pragma: no cover - optional until dependencies are installed
    BackgroundScheduler = None

from .broker import AlpacaBroker, BrokerError, TelegramNotifier
from . import config
from .restrictions import RejectedOrder, get_tracker
from .signal_engine import analyze_signal, compute_atr, run_alphacore_pipeline
from .storage import record_agent_event, record_signal_event, record_trade_event, trade_summary
from .strategy import BaseStrategy, get_strategy

logger = logging.getLogger("trading_agent")
logging.basicConfig(level=logging.INFO)

_event_listeners = []


def register_agent_event_listener(listener) -> None:
    _event_listeners.append(listener)


def emit_agent_event(event_type: str, payload: dict) -> None:
    for listener in list(_event_listeners):
        try:
            listener(event_type, payload)
        except Exception:
            logger.exception("NexoSignal Agent realtime listener failed")


class NexoSignalAgent:
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

    def _log(self, msg: str, level: str = "info", event_type: str = "agent_log", symbol: str | None = None, payload: dict | None = None) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        entry = {"time": ts, "msg": msg, "level": level}
        self.log.append(entry)
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

    def evaluate_symbol(self, symbol: str) -> Optional[str]:
        """Fetch bars, run strategy, return order id or None."""
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

    def reset_session(self) -> None:
        self.strike_count = 0
        self.circuit_breaker_active = False
        self.autonomous_orders.clear()
        self.session_trade_attempts = 0
        self.trade_floor_logged = False
        self._log("NexoSignal Agent session reset; circuit breaker inactive", event_type="session_reset")
        emit_agent_event("circuit_breaker_update", {"active": False, "strike_count": 0, "reason": "session reset"})

    def run_market_open_scan(self) -> None:
        """9:30 AM NexoSignal Agent job."""
        self.reset_session()
        self._log("NexoSignal Agent market-open job fired", event_type="schedule_job")
        if not self.broker.is_market_open():
            self._log("NexoSignal Agent stopped market-open scan because Alpaca clock is closed", "warning", "market_closed")
            return
        try:
            picks = asyncio.run(
                run_alphacore_pipeline(
                    snapshot=asyncio.run(self.broker.get_bulk_snapshots()),
                    headers=self.broker._headers,
                    data_base=self.broker._data_base,
                    emit_update=lambda payload: self._emit_confluence(payload),
                    max_candidates=5,
                )
            )
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            snapshot = loop.run_until_complete(self.broker.get_bulk_snapshots())
            picks = loop.run_until_complete(
                run_alphacore_pipeline(
                    snapshot=snapshot,
                    headers=self.broker._headers,
                    data_base=self.broker._data_base,
                    emit_update=lambda payload: self._emit_confluence(payload),
                    max_candidates=5,
                )
            )
            loop.close()
        self._log(f"NexoSignal AlphaCore produced {len(picks)} alpha picks", event_type="alphacore_picks", payload={"count": len(picks)})
        self.notifier.daily_picks_alert(picks)
        for pick in picks:
            self.execute_alpha_pick(pick)

    def _emit_confluence(self, payload: dict) -> None:
        msg = (
            f"NexoSignal AlphaCore scored {payload['symbol']} "
            f"confluence={payload['confluence_score']:.2f} imbalance={payload['order_book_imbalance']:.4f}"
        )
        self._log(msg, event_type="confluence_update", symbol=payload["symbol"], payload=payload)
        emit_agent_event("confluence_update", payload)

    def execute_alpha_pick(self, pick) -> None:
        """NexoSignal Executor bracket-order flow."""
        if self.circuit_breaker_active:
            self._log(f"NexoSignal Guard blocked {pick.symbol}: circuit breaker active", "warning", "execution_blocked", pick.symbol)
            return
        account = self.broker.get_account()
        settled_cash = float(account.get("cash", 0))
        qty = math.floor((settled_cash * config.POSITION_SIZE_PCT) / max(pick.price, 0.01))
        if qty <= 0:
            self._log(f"NexoSignal Guard blocked {pick.symbol}: insufficient settled cash", "warning", "execution_blocked", pick.symbol)
            return
        stop_loss = pick.price - pick.atr
        take_profit = pick.price + (5 * pick.atr)
        rr = ((take_profit - pick.price) / max(pick.price - stop_loss, 0.0001)) if stop_loss < pick.price else 0
        try:
            order = self.broker.place_bracket_order(pick.symbol, qty, pick.price, stop_loss, take_profit)
            self.session_trade_attempts += 1
            self.autonomous_orders[pick.symbol] = {
                "entry_price": pick.price,
                "qty": qty,
                "atr": pick.atr,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "break_even_moved": False,
                "order_id": order.get("id"),
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
            self._log(f"NexoSignal Executor submitted bracket order for {pick.symbol}", event_type="order_submitted", symbol=pick.symbol, payload={"qty": qty, "stop_loss": stop_loss, "take_profit": take_profit})
            self.notifier.trade_opened(pick.symbol, pick.price, qty, stop_loss, take_profit)
        except (BrokerError, RejectedOrder) as exc:
            self._log(f"NexoSignal Executor failed {pick.symbol}: {exc}", "error", "order_error", pick.symbol)

    def heartbeat(self) -> None:
        """60-second NexoSignal Agent heartbeat."""
        self._log("NexoSignal Agent heartbeat fired", event_type="schedule_job")
        self.check_trade_floor()
        self.check_positions()

    def check_trade_floor(self) -> None:
        now_et = datetime.now(pytz.timezone("America/New_York")).time()
        if self.trade_floor_logged or now_et < clock_time(15, 0):
            return
        if self.session_trade_attempts < 3:
            msg = f"trade floor not met: {self.session_trade_attempts}/3 qualifying setups by 3:00 PM"
            self.trade_floor_logged = True
            self._log(msg, "warning", "trade_floor_not_met", payload={"attempts": self.session_trade_attempts})

    def check_positions(self) -> None:
        for position in self.broker.get_positions():
            symbol = position.get("symbol")
            tracked = self.autonomous_orders.get(symbol)
            if not tracked:
                continue
            current_price = float(position.get("current_price") or position.get("market_value", 0)) / max(float(position.get("qty", 1)), 1)
            entry = tracked["entry_price"]
            atr = max(tracked["atr"], 0.0001)
            if not tracked["break_even_moved"] and current_price >= entry + (2 * atr):
                tracked["stop_loss"] = entry
                tracked["break_even_moved"] = True
                self._log(f"NexoSignal Executor moved {symbol} stop to break-even", event_type="break_even_stop", symbol=symbol)
                self.notifier.stop_adjusted(symbol, entry, current_price, (current_price - entry) / atr)
            payload = {
                "symbol": symbol,
                "entry_price": entry,
                "current_price": current_price,
                "stop_loss": tracked["stop_loss"],
                "take_profit": tracked["take_profit"],
                "atr": atr,
            }
            emit_agent_event("position_update", payload)
            self._log(f"NexoSignal Agent position update {symbol}", event_type="position_update", symbol=symbol, payload=payload)
            if float(position.get("market_value", 0)) > config.MAX_POSITION_SIZE:
                self.add_strike(f"{symbol} position exceeds MAX_POSITION_SIZE")

    def add_strike(self, reason: str) -> None:
        self.strike_count += 1
        self._log(f"NexoSignal Guard strike {self.strike_count}/2: {reason}", "warning", "guard_strike", payload={"reason": reason})
        if self.strike_count >= 2 and not self.circuit_breaker_active:
            self.trip_circuit_breaker(reason)

    def trip_circuit_breaker(self, reason: str) -> None:
        self.circuit_breaker_active = True
        self.broker.cancel_all_orders()
        self._log(f"NexoSignal Guard circuit breaker tripped: {reason}", "error", "circuit_breaker", payload={"reason": reason})
        self.notifier.circuit_breaker_tripped(reason)
        emit_agent_event("circuit_breaker_update", {"active": True, "strike_count": self.strike_count, "reason": reason})

    def end_of_day_report(self) -> None:
        summary = trade_summary()
        self._log("NexoSignal Agent end-of-day report fired", event_type="schedule_job", payload=summary)
        self.notifier.eod_report(
            trades=int(summary.get("total") or 0),
            wins=int(summary.get("accepted") or 0),
            losses=int(summary.get("failed") or 0),
            pnl=0.0,
            breaker_active=self.circuit_breaker_active,
            strikes=self.strike_count,
        )

    def run_once(self) -> None:
        """Single scan across all watched symbols."""
        if not self.broker.is_market_open():
            self._log("Market is closed, skipping scan.")
            return

        tracker = get_tracker()
        self._log(
            f"Scan - strategy={self.strategy.name} | "
            f"trades today={tracker.trades_today}/{config.MAX_TRADES_PER_DAY}"
        )

        for symbol in self.symbols:
            self.evaluate_symbol(symbol)

    def run(self) -> None:
        """Block and run the agent loop indefinitely."""
        self._running = True
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
                time.sleep(self.poll_interval)
        except KeyboardInterrupt:
            self._log("Agent stopped by user.")
            self._running = False

    def stop(self) -> None:
        self._running = False
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
            }
        except BrokerError as e:
            return {"error": str(e)}


TradingAgent = NexoSignalAgent


def start_nexosignal_scheduler(agent: NexoSignalAgent):
    if BackgroundScheduler is None:
        raise RuntimeError("APScheduler is not installed. Install requirements.txt before starting the scheduler.")
    eastern = pytz.timezone("America/New_York")
    scheduler = BackgroundScheduler(timezone=eastern)
    scheduler.add_job(agent.run_market_open_scan, "cron", day_of_week="mon-fri", hour=9, minute=30, id="nexosignal_market_open")
    scheduler.add_job(agent.heartbeat, "interval", seconds=60, id="nexosignal_heartbeat")
    scheduler.add_job(agent.end_of_day_report, "cron", day_of_week="mon-fri", hour=16, minute=0, id="nexosignal_eod")
    scheduler.start()
    agent._log("NexoSignal Agent scheduler started", event_type="scheduler_started")
    return scheduler


if __name__ == "__main__":
    agent = NexoSignalAgent(config.DEFAULT_DASHBOARD_SYMBOLS, dry_run=True)
    scheduler = start_nexosignal_scheduler(agent)
    try:
        while True:
            time.sleep(5)
    except KeyboardInterrupt:
        scheduler.shutdown()
        agent.stop()
