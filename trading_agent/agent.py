"""
Trading agent loop. Runs on a configurable interval, evaluates the strategy
for each watched symbol, and places orders when signalled.
"""

import logging
import math
import asyncio
import threading
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
from .restrictions import (
    RejectedOrder,
    calculate_position_risk,
    check_correlation,
    check_portfolio_var,
    get_tracker,
)
from .signal_engine import (
    NexoSignalAlphaCoreCandidate,
    analyze_signal,
    alphacore_features,
    compute_confluence_score,
    compute_vwap,
)
from .storage import (
    record_agent_event, record_signal_event, record_trade_event, trade_summary,
    upsert_watchlist_entry, record_macro_snapshot, record_insider_activity,
    list_watchlist, list_insider_activity,
    record_performance_event, record_risk_metrics,
)
from .strategy import (
    BaseStrategy,
    asset_type_for_symbol,
    calculate_bl_weights,
    detect_market_regime,
    get_strategy,
    normalize_symbol,
    rebuild_watchlist,
    should_skip_for_earnings,
)

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
        self.started_at = time.time()

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
        picks = self._run_market_open_pipeline()
        self._log(f"NexoSignal AlphaCore selected {len(picks)} daily alpha picks", event_type="alphacore_picks", payload={"count": len(picks)})
        self.notifier.daily_picks_alert(picks)
        regime = detect_market_regime(config.FRED_API_KEY).get("regime", "neutral")
        weights = calculate_bl_weights(picks, regime)
        for pick in picks:
            self.execute_alpha_pick(pick, allocation_weight=weights.get(pick.symbol, 1 / max(len(picks), 1)))

    async def _market_open_pipeline(self) -> list[NexoSignalAlphaCoreCandidate]:
        return self._build_daily_top_picks()

    def _build_daily_top_picks(self) -> list[NexoSignalAlphaCoreCandidate]:
        watchlist = list_watchlist(50)
        symbols = [normalize_symbol(str(row.get("symbol", ""))) for row in watchlist if row.get("symbol")]
        if not symbols:
            symbols = [normalize_symbol(s) for s in self.symbols]
            self._log("NexoSignal AlphaCore using dashboard symbols because watchlist is empty", "warning", "watchlist_empty")

        candidates: list[NexoSignalAlphaCoreCandidate] = []
        for symbol in dict.fromkeys(symbols):
            asset_type = asset_type_for_symbol(symbol)
            should_skip, reason = should_skip_for_earnings(symbol)
            if should_skip:
                self._log(
                    f"NexoSignal AlphaCore omitted {symbol}: {reason}",
                    "warning",
                    "earnings_filter_skip",
                    symbol,
                    {"asset_type": asset_type, "reason": reason},
                )
                continue
            if asset_type == "crypto":
                self._log(
                    f"NexoSignal AlphaCore routed {symbol} as crypto; corporate earnings check bypassed",
                    event_type="asset_routed",
                    symbol=symbol,
                    payload={"asset_type": asset_type},
                )

            try:
                bars = self.broker.get_bars(symbol, timeframe="1Day", limit=80)
                if len(bars) < 50:
                    self._log(f"{symbol}: insufficient bars for AlphaCore ({len(bars)}/50)", "warning", "alphacore_skip", symbol)
                    continue
                confluence = compute_confluence_score(bars)
                if confluence < config.SIGNAL_MIN_CONFIDENCE:
                    self._emit_confluence({
                        "symbol": symbol,
                        "confluence_score": round(confluence, 2),
                        "order_book_imbalance": 0.0,
                        "asset_type": asset_type,
                    })
                    continue
                features = alphacore_features(bars)
                price = float(bars[-1]["c"])
                probability = max(0.01, min(0.99, confluence / 100.0))
                candidate = NexoSignalAlphaCoreCandidate(
                    symbol=symbol,
                    price=round(price, 4),
                    volume=float(bars[-1].get("v") or 0),
                    confluence_score=round(confluence, 2),
                    order_book_imbalance=0.0,
                    probability=round(probability, 4),
                    atr=round(features["atr"], 4),
                    vwap=round(compute_vwap(bars), 4),
                    rsi=round(features["rsi"], 2),
                    sma_slope_delta=round(features["sma_slope_delta"], 4),
                    ema_trend_delta=round(features["ema_trend_delta"], 4),
                    macd_histogram=round(features["macd_histogram"], 4),
                    liquidity_score=70.0,
                    spread_bps=0.0,
                    slippage_estimate_bps=0.0,
                )
                candidates.append(candidate)
                self._emit_confluence({
                    "symbol": symbol,
                    "confluence_score": candidate.confluence_score,
                    "order_book_imbalance": candidate.order_book_imbalance,
                    "asset_type": asset_type,
                })
            except BrokerError as exc:
                self._log(f"NexoSignal AlphaCore skipped {symbol}: broker data error {exc}", "warning", "alphacore_skip", symbol)
            except Exception as exc:
                self._log(f"NexoSignal AlphaCore skipped {symbol}: {exc}", "error", "alphacore_error", symbol)

        top = sorted(candidates, key=lambda c: c.confluence_score, reverse=True)[:3]
        if len(top) < 3:
            self._log(
                f"NexoSignal AlphaCore found only {len(top)} eligible picks above threshold",
                "warning",
                "alphacore_top3_shortfall",
                payload={"eligible": len(top), "threshold": config.SIGNAL_MIN_CONFIDENCE},
            )
        return top

    def _run_market_open_pipeline(self):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self._market_open_pipeline())

        result: dict[str, object] = {}

        def _runner() -> None:
            try:
                result["picks"] = asyncio.run(self._market_open_pipeline())
            except Exception as exc:
                result["error"] = exc

        thread = threading.Thread(target=_runner, daemon=True)
        thread.start()
        thread.join()
        if "error" in result:
            raise result["error"]  # type: ignore[misc]
        return result.get("picks", [])

    def _emit_confluence(self, payload: dict) -> None:
        msg = (
            f"NexoSignal AlphaCore scored {payload['symbol']} "
            f"confluence={payload['confluence_score']:.2f} imbalance={payload['order_book_imbalance']:.4f}"
        )
        self._log(msg, event_type="confluence_update", symbol=payload["symbol"], payload=payload)
        emit_agent_event("confluence_update", payload)
        self._record_telemetry(
            stage="alphacore_confluence",
            symbol=payload["symbol"],
            slippage_bps=payload.get("spread_bps"),
            payload=payload,
        )

    def execute_alpha_pick(self, pick, allocation_weight: float | None = None) -> None:
        """NexoSignal Executor bracket-order flow."""
        started = time.perf_counter()
        asset_type = asset_type_for_symbol(pick.symbol)
        if self.circuit_breaker_active:
            self._log(f"NexoSignal Guard blocked {pick.symbol}: circuit breaker active", "warning", "execution_blocked", pick.symbol)
            return
        if asset_type == "crypto":
            self._log(
                f"NexoSignal Guard blocked {pick.symbol}: crypto bracket execution is not enabled in the safe initial rollout",
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
            self._log(f"NexoSignal Guard blocked {pick.symbol}: insufficient settled cash", "warning", "execution_blocked", pick.symbol)
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
            self._log(f"NexoSignal Guard blocked {pick.symbol}: {exc}", "warning", "execution_blocked", pick.symbol)
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
            self._log(f"NexoSignal Executor failed {pick.symbol}: {exc}", "error", "order_error", pick.symbol)

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

    def heartbeat(self) -> None:
        """60-second NexoSignal Agent heartbeat."""
        self._log("NexoSignal Agent heartbeat fired", event_type="schedule_job")
        self.check_trade_floor()
        self.check_positions()
        self._record_session_telemetry("heartbeat")

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
        self._record_session_telemetry("end_of_day")
        self.notifier.eod_report(
            trades=int(summary.get("total") or 0),
            wins=int(summary.get("accepted") or 0),
            losses=int(summary.get("failed") or 0),
            pnl=0.0,
            breaker_active=self.circuit_breaker_active,
            strikes=self.strike_count,
        )

    def _record_session_telemetry(self, stage: str) -> None:
        try:
            summary = trade_summary()
            total = int(summary.get("total") or 0)
            accepted = int(summary.get("accepted") or 0)
            failed = int(summary.get("failed") or 0)
            win_rate = (accepted / total * 100) if total else 0.0
            realized_pnl = -float(failed)
            mtm_pnl = sum(float(p.get("unrealized_pl", 0)) for p in self.broker.get_positions())
            self._record_telemetry(
                stage=stage,
                mark_to_market_pnl=round(mtm_pnl, 2),
                realized_pnl=round(realized_pnl, 2),
                win_rate=round(win_rate, 2),
                trade_count=total,
                payload={"accepted": accepted, "failed": failed, "dry_run": self.dry_run},
            )
        except Exception:
            logger.exception("NexoSignal telemetry snapshot failed")

    def _record_telemetry(
        self,
        *,
        stage: str,
        symbol: str | None = None,
        latency_ms: float | None = None,
        slippage_bps: float | None = None,
        mark_to_market_pnl: float | None = None,
        realized_pnl: float | None = None,
        win_rate: float | None = None,
        trade_count: int | None = None,
        payload: dict | None = None,
    ) -> None:
        try:
            record_performance_event(
                stage=stage,
                symbol=symbol,
                latency_ms=round(latency_ms, 2) if latency_ms is not None else None,
                slippage_bps=round(slippage_bps, 2) if slippage_bps is not None else None,
                mark_to_market_pnl=mark_to_market_pnl,
                realized_pnl=realized_pnl,
                win_rate=win_rate,
                trade_count=trade_count,
                uptime_seconds=int(time.time() - self.started_at),
                payload=payload,
            )
        except Exception:
            logger.exception("NexoSignal Ledger telemetry write failed")

    # ── Phase 2: Intelligence Extension scheduled jobs ─────────────────────

    def scout_rebuild(self) -> None:
        """Saturday 8 AM — NexoSignal Scout rebuilds the watchlist from FMP."""
        self._log("NexoSignal Scout watchlist rebuild started", event_type="schedule_job")
        entries = rebuild_watchlist(config.FMP_API_KEY, config.WATCHLIST_SIZE)
        if not entries:
            self._log("NexoSignal Scout: no entries returned (FMP_API_KEY may be unset)", "warning")
            return
        for entry in entries:
            try:
                upsert_watchlist_entry(
                    symbol=entry["symbol"],
                    composite_score=entry["composite_score"],
                    category=entry["category"],
                    rank_position=entry["rank_position"],
                    score_growth=entry.get("score_growth", 0.0),
                    score_value=entry.get("score_value", 0.0),
                    score_yield=entry.get("score_yield", 0.0),
                    score_sentiment=entry.get("score_sentiment", 0.0),
                    score_insider=entry.get("score_insider", 50.0),
                    score_earnings_quality=entry.get("score_earnings_quality", 0.0),
                )
            except Exception:
                logger.exception("Scout: failed to persist watchlist entry for %s", entry.get("symbol"))
        self._log(
            f"NexoSignal Scout watchlist rebuilt: {len(entries)} symbols",
            event_type="watchlist_rebuilt",
            payload={"count": len(entries)},
        )
        emit_agent_event("watchlist_rebuilt", {"count": len(entries), "top3": [e["symbol"] for e in entries[:3]]})

    def macro_refresh(self) -> None:
        """Sunday 8 AM — NexoSignal Lens fetches macro regime from FRED."""
        self._log("NexoSignal Lens macro refresh started", event_type="schedule_job")
        result = detect_market_regime(config.FRED_API_KEY)
        try:
            record_macro_snapshot(
                dgs10=result.get("dgs10"),
                dgs2=result.get("dgs2"),
                spread=result.get("spread"),
                jobless_claims=result.get("jobless_claims"),
                regime=result["regime"],
            )
        except Exception:
            logger.exception("Lens: failed to persist macro snapshot")
        self._log(
            f"NexoSignal Lens macro regime: {result['regime'].upper()}",
            event_type="macro_refreshed",
            payload=result,
        )
        emit_agent_event("macro_refreshed", result)

    def premarket_briefing(self) -> None:
        """Weekday 7 AM — sends pre-market Telegram intelligence briefing."""
        self._log("NexoSignal pre-market briefing job fired", event_type="schedule_job")
        regime = detect_market_regime(config.FRED_API_KEY)
        watchlist = list_watchlist(10)
        insiders = list_insider_activity(5)

        self._log(
            f"NexoSignal pre-market: regime={regime['regime']} watchlist={len(watchlist)} insiders={len(insiders)}",
            event_type="premarket_briefing",
        )
        self.notifier.send_premarket_briefing(
            picks=[],
            regime=regime,
            macro=None,
            insiders=insiders,
        )

    def insider_parse(self) -> None:
        """Weekday 6 PM — NexoSignal Lens parses SEC EDGAR Form 4 filings."""
        from .ai_research import parse_insider_filings

        self._log("NexoSignal Lens insider parse job fired", event_type="schedule_job")
        watchlist = list_watchlist(config.WATCHLIST_SIZE)
        if not watchlist:
            self._log("Lens: watchlist empty — run Scout first (Sat 8 AM)", "warning")
            return

        total_saved = 0
        for entry in watchlist[:20]:
            sym = entry.get("symbol", "")
            if not sym:
                continue
            filings = parse_insider_filings(sym, max_filings=5)
            for filing in filings:
                try:
                    record_insider_activity(
                        symbol=sym,
                        insider_name=filing.get("insider_name"),
                        title=filing.get("title"),
                        transaction_type=filing.get("transaction_type"),
                        shares=filing.get("shares"),
                        value=filing.get("value"),
                        filed_at=filing.get("filed_at", datetime.utcnow().isoformat()),
                    )
                    total_saved += 1
                except Exception:
                    logger.exception("Lens: failed to persist insider filing for %s", sym)

        self._log(
            f"NexoSignal Lens insider parse complete: {total_saved} filings saved",
            event_type="insider_parsed",
            payload={"total_saved": total_saved},
        )
        emit_agent_event("insider_parsed", {"total_saved": total_saved})

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

    # ── Existing jobs ──────────────────────────────────────────────────────
    scheduler.add_job(agent.run_market_open_scan, "cron", day_of_week="mon-fri", hour=9, minute=30, id="nexosignal_market_open")
    scheduler.add_job(agent.heartbeat, "interval", seconds=60, id="nexosignal_heartbeat")
    scheduler.add_job(agent.end_of_day_report, "cron", day_of_week="mon-fri", hour=16, minute=0, id="nexosignal_eod")

    # ── Phase 2: Intelligence Extension jobs ──────────────────────────────
    scheduler.add_job(agent.scout_rebuild, "cron", day_of_week="sat", hour=8, minute=0, id="nexosignal_scout_rebuild")
    scheduler.add_job(agent.macro_refresh, "cron", day_of_week="sun", hour=8, minute=0, id="nexosignal_macro_refresh")
    scheduler.add_job(agent.premarket_briefing, "cron", day_of_week="mon-fri", hour=7, minute=0, id="nexosignal_premarket")
    scheduler.add_job(agent.insider_parse, "cron", day_of_week="mon-fri", hour=18, minute=0, id="nexosignal_insider_parse")

    scheduler.start()
    agent._log("NexoSignal Agent scheduler started (Phase 2: Scout + Lens + Guard)", event_type="scheduler_started")
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
