"""NexoSignal scheduled-job methods extracted from agent.py.

``NexoSignalJobsMixin`` contains every APScheduler-driven method so that the
core ``NexoSignalAgent`` (scan loop + order execution) stays focused and short.

All methods reference ``self`` attributes that are set by ``NexoSignalAgent.__init__``:
  broker, notifier, symbols, dry_run, autonomous_orders,
  session_trade_attempts, trade_floor_logged, strike_count,
  circuit_breaker_active, started_at, _stop_event
"""

from __future__ import annotations

import asyncio
import logging
import math
import threading
import time
from datetime import datetime, time as clock_time
from typing import TYPE_CHECKING

import pytz

from . import config
from .broker import BrokerError
from .events import emit_agent_event
from .signal_engine import (
    NexoSignalAlphaCoreCandidate,
    NexoSignalAlphaCoreModel,
    ALPHACORE_FEATURE_KEYS,
    alphacore_features,
    compute_confluence_score,
    compute_vwap,
    default_alphacore_weights,
)
from .storage import (
    list_insider_activity,
    list_watchlist,
    record_insider_activity,
    record_macro_snapshot,
    record_performance_event,
    trade_summary,
    upsert_watchlist_entry,
)
from .strategy import (
    asset_type_for_symbol,
    calculate_bl_weights,
    detect_market_regime,
    normalize_symbol,
    rebuild_watchlist,
    should_skip_for_earnings,
)

if TYPE_CHECKING:
    from .broker import AlpacaBroker

logger = logging.getLogger("trading_agent")


# ── XGBoost model loader (sync, broker-based) ─────────────────────────────────

def _load_or_train_model_sync(
    broker: "AlpacaBroker",
    symbols: list[str],
) -> NexoSignalAlphaCoreModel:
    """Load a cached AlphaCore model or train a new one using broker daily bars.

    Persists the model to disk so subsequent calls within 24 h skip retraining.
    Falls back to a weighted-confluence model when XGBoost is unavailable.
    """
    import os
    import pickle
    import time as _time
    from statistics import mean as _mean

    model_path = config.ALPHACORE_MODEL_PATH
    if os.path.exists(model_path) and _time.time() - os.path.getmtime(model_path) < 24 * 3600:
        try:
            with open(model_path, "rb") as fh:
                return pickle.load(fh)
        except Exception:
            pass

    keys = list(ALPHACORE_FEATURE_KEYS)
    rows: list[dict] = []
    labels: list[int] = []
    for sym in symbols[:20]:
        try:
            bars = broker.get_bars(sym, timeframe="1Day", limit=120)
        except Exception:
            continue
        if len(bars) < 35:
            continue
        for i in range(30, len(bars) - 1):
            window = bars[max(0, i - 30): i + 1]
            features = alphacore_features(window)
            rows.append(features)
            labels.append(1 if float(bars[i + 1]["c"]) > float(bars[i]["c"]) else 0)

    means: dict[str, float] = {k: 0.0 for k in keys}
    weights = default_alphacore_weights()
    xgb_model = None

    if rows:
        means = {k: _mean(r[k] for r in rows) for k in keys}
        try:
            from xgboost import XGBClassifier
            clf = XGBClassifier(
                n_estimators=30,
                max_depth=3,
                learning_rate=0.1,
                eval_metric="logloss",
            )
            clf.fit([[r[k] for k in keys] for r in rows], labels)
            weights = {k: float(clf.feature_importances_[i]) for i, k in enumerate(keys)}
            xgb_model = clf
        except Exception:
            pass

    model = NexoSignalAlphaCoreModel(
        means=means,
        weights=weights,
        xgb_model=xgb_model,
        feature_keys=tuple(keys),
    )
    try:
        import pickle as _pickle
        with open(model_path, "wb") as fh:
            _pickle.dump(model, fh)
    except Exception:
        pass
    return model


# ── Mixin ─────────────────────────────────────────────────────────────────────

class NexoSignalJobsMixin:
    """All APScheduler-driven methods for NexoSignalAgent.

    Expects the host class to provide:
      self.broker, self.notifier, self.symbols, self.dry_run,
      self.autonomous_orders, self.session_trade_attempts,
      self.trade_floor_logged, self.strike_count,
      self.circuit_breaker_active, self.started_at
    And the host's ``_log(msg, level, event_type, symbol, payload)`` method.
    """

    # ── Session ───────────────────────────────────────────────────────────────

    def reset_session(self) -> None:
        self.strike_count = 0
        self.circuit_breaker_active = False
        self.autonomous_orders.clear()
        self.session_trade_attempts = 0
        self.trade_floor_logged = False
        self._log(
            "NexoSignal Agent session reset; circuit breaker inactive",
            event_type="session_reset",
        )
        emit_agent_event("circuit_breaker_update", {
            "active": False,
            "strike_count": 0,
            "reason": "session reset",
        })

    # ── Guard / position monitoring ───────────────────────────────────────────

    def add_strike(self, reason: str) -> None:
        self.strike_count += 1
        self._log(
            f"NexoSignal Guard strike {self.strike_count}/2: {reason}",
            "warning",
            "guard_strike",
            payload={"reason": reason},
        )
        if self.strike_count >= 2 and not self.circuit_breaker_active:
            self.trip_circuit_breaker(reason)

    def trip_circuit_breaker(self, reason: str) -> None:
        self.circuit_breaker_active = True
        if not self.dry_run:
            self.broker.cancel_all_orders()
        self._log(
            f"NexoSignal Guard circuit breaker tripped: {reason}",
            "error",
            "circuit_breaker",
            payload={"reason": reason},
        )
        emit_agent_event("circuit_breaker_update", {
            "active": True,
            "strike_count": self.strike_count,
            "reason": reason,
        })

    def check_trade_floor(self) -> None:
        now_et = datetime.now(pytz.timezone("America/New_York")).time()
        if self.trade_floor_logged or now_et < clock_time(15, 0):
            return
        if self.session_trade_attempts < 3:
            msg = (
                f"trade floor not met: {self.session_trade_attempts}/3 "
                "qualifying setups by 3:00 PM"
            )
            self.trade_floor_logged = True
            self._log(msg, "warning", "trade_floor_not_met", payload={"attempts": self.session_trade_attempts})

    def check_positions(self) -> None:
        for position in self.broker.get_positions():
            symbol = position.get("symbol")
            tracked = self.autonomous_orders.get(symbol)
            if not tracked:
                continue
            current_price = (
                float(position.get("current_price") or position.get("market_value", 0))
                / max(float(position.get("qty", 1)), 1)
            )
            entry = tracked["entry_price"]
            atr = max(tracked["atr"], 0.0001)
            if not tracked["break_even_moved"] and current_price >= entry + (2 * atr):
                tracked["stop_loss"] = entry
                tracked["break_even_moved"] = True
                self._log(
                    f"NexoSignal Executor moved {symbol} stop to break-even @ ${entry:.2f}",
                    event_type="break_even_stop",
                    symbol=symbol,
                )
                self.notifier.stop_adjusted(symbol, entry, current_price, (current_price - entry) / atr)
                stop_order_id = tracked.get("stop_order_id")
                if stop_order_id:
                    try:
                        self.broker.replace_order(stop_order_id, stop_price=entry)
                        self._log(
                            f"NexoSignal Executor updated {symbol} stop order to ${entry:.2f}",
                            event_type="stop_replaced",
                            symbol=symbol,
                        )
                    except Exception as exc:
                        self._log(
                            f"NexoSignal Executor could not update {symbol} stop order: {exc}",
                            "warning",
                            "stop_replace_failed",
                            symbol,
                        )
            payload = {
                "symbol": symbol,
                "entry_price": entry,
                "current_price": current_price,
                "stop_loss": tracked["stop_loss"],
                "take_profit": tracked["take_profit"],
                "atr": atr,
            }
            emit_agent_event("position_update", payload)
            self._log(
                f"NexoSignal Agent position update {symbol}",
                event_type="position_update",
                symbol=symbol,
                payload=payload,
            )
            if float(position.get("market_value", 0)) > config.MAX_POSITION_SIZE:
                self.add_strike(f"{symbol} position exceeds MAX_POSITION_SIZE")

    # ── Market-open pipeline ──────────────────────────────────────────────────

    def heartbeat(self) -> None:
        self._log("NexoSignal Agent heartbeat fired", event_type="schedule_job")
        self.check_trade_floor()
        self.check_positions()
        self._record_session_telemetry("heartbeat")

    def run_market_open_scan(self) -> None:
        """9:30 AM ET — reset session and fire AlphaCore picks pipeline."""
        self.reset_session()
        self._log("NexoSignal Agent market-open job fired", event_type="schedule_job")
        if not self.broker.is_market_open():
            self._log(
                "NexoSignal Agent stopped market-open scan because Alpaca clock is closed",
                "warning",
                "market_closed",
            )
            return
        picks = self._run_market_open_pipeline()
        self._log(
            f"NexoSignal AlphaCore selected {len(picks)} daily alpha picks",
            event_type="alphacore_picks",
            payload={"count": len(picks)},
        )
        self.notifier.daily_picks_alert(picks)
        regime = detect_market_regime(config.FRED_API_KEY).get("regime", "neutral")
        weights = calculate_bl_weights(picks, regime)
        for pick in picks:
            self.execute_alpha_pick(
                pick,
                allocation_weight=weights.get(pick.symbol, 1 / max(len(picks), 1)),
            )

    async def _market_open_pipeline(self) -> list[NexoSignalAlphaCoreCandidate]:
        return self._build_daily_top_picks()

    def _run_market_open_pipeline(self) -> list[NexoSignalAlphaCoreCandidate]:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self._market_open_pipeline())

        result: dict = {}

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

    def _build_daily_top_picks(self) -> list[NexoSignalAlphaCoreCandidate]:
        """Two-pass AlphaCore scoring: filter → XGBoost → top-3."""
        watchlist = list_watchlist(50)
        symbols = [normalize_symbol(str(row.get("symbol", ""))) for row in watchlist if row.get("symbol")]
        if not symbols:
            symbols = [normalize_symbol(s) for s in self.symbols]
            self._log(
                "NexoSignal AlphaCore using dashboard symbols because watchlist is empty",
                "warning",
                "watchlist_empty",
            )

        # Pass 1 — filter and collect eligible symbols with bars + features
        eligible: list[tuple[str, list[dict], float, dict]] = []
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
                    self._log(
                        f"{symbol}: insufficient bars for AlphaCore ({len(bars)}/50)",
                        "warning",
                        "alphacore_skip",
                        symbol,
                    )
                    continue
                confluence = compute_confluence_score(bars)
                confluence_payload = {
                    "symbol": symbol,
                    "confluence_score": round(confluence, 2),
                    "order_book_imbalance": 0.0,
                    "asset_type": asset_type,
                }
                self._emit_confluence(confluence_payload)
                if confluence < config.SIGNAL_MIN_CONFIDENCE:
                    continue
                features = alphacore_features(bars)
                eligible.append((symbol, bars, confluence, features))
            except BrokerError as exc:
                self._log(
                    f"NexoSignal AlphaCore skipped {symbol}: broker data error {exc}",
                    "warning",
                    "alphacore_skip",
                    symbol,
                )
            except Exception as exc:
                self._log(
                    f"NexoSignal AlphaCore skipped {symbol}: {exc}",
                    "error",
                    "alphacore_error",
                    symbol,
                )

        if not eligible:
            self._log(
                "NexoSignal AlphaCore found no eligible symbols above threshold",
                "warning",
                "alphacore_top3_shortfall",
                payload={"eligible": 0, "threshold": config.SIGNAL_MIN_CONFIDENCE},
            )
            return []

        # Pass 2 — load/train XGBoost model, score all eligible symbols
        model: NexoSignalAlphaCoreModel | None = None
        try:
            model = _load_or_train_model_sync(self.broker, [s for s, _, _, _ in eligible])
            mode = "XGBoost" if model.xgb_model else "weighted-confluence"
            self._log(f"NexoSignal AlphaCore model ready: {mode}", event_type="alphacore_model")
        except Exception as exc:
            self._log(
                f"NexoSignal AlphaCore model unavailable, using confluence fallback: {exc}",
                "warning",
                "alphacore_model_error",
            )

        candidates: list[NexoSignalAlphaCoreCandidate] = []
        for symbol, bars, confluence, features in eligible:
            price = float(bars[-1]["c"])
            if model is not None:
                try:
                    probability = model.predict_probability(features)
                except Exception:
                    probability = max(0.01, min(0.99, confluence / 100.0))
            else:
                probability = max(0.01, min(0.99, confluence / 100.0))
            candidates.append(NexoSignalAlphaCoreCandidate(
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
            ))

        top = sorted(candidates, key=lambda c: (c.probability, c.confluence_score), reverse=True)[:3]
        if len(top) < 3:
            self._log(
                f"NexoSignal AlphaCore found only {len(top)} eligible picks above threshold",
                "warning",
                "alphacore_top3_shortfall",
                payload={"eligible": len(top), "threshold": config.SIGNAL_MIN_CONFIDENCE},
            )
        return top

    def _emit_confluence(self, payload: dict) -> None:
        msg = (
            f"NexoSignal AlphaCore scored {payload['symbol']} "
            f"confluence={payload['confluence_score']:.2f} "
            f"imbalance={payload['order_book_imbalance']:.4f}"
        )
        self._log(msg, event_type="confluence_update", symbol=payload["symbol"], payload=payload)
        emit_agent_event("confluence_update", payload)
        self._record_telemetry(
            stage="alphacore_confluence",
            symbol=payload["symbol"],
            slippage_bps=payload.get("spread_bps"),
            payload=payload,
        )

    # ── Telemetry helpers ─────────────────────────────────────────────────────

    def end_of_day_report(self) -> None:
        summary = trade_summary()
        self._log(
            "NexoSignal Agent end-of-day report fired",
            event_type="schedule_job",
            payload=summary,
        )
        self._record_session_telemetry("end_of_day")
        try:
            positions = self.broker.get_positions()
            mtm_pnl = round(sum(float(p.get("unrealized_pl", 0)) for p in positions), 2)
        except Exception:
            mtm_pnl = 0.0
        self.notifier.eod_report(
            trades=int(summary.get("total") or 0),
            wins=int(summary.get("accepted") or 0),
            losses=int(summary.get("failed") or 0),
            pnl=mtm_pnl,
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
            mtm_pnl = sum(
                float(p.get("unrealized_pl", 0)) for p in self.broker.get_positions()
            )
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

    # ── Phase 2: Intelligence Extension ──────────────────────────────────────

    def _compute_insider_score(self, symbol: str) -> float:
        """Derive insider sentiment score (0-100) from recent SEC Form 4 filings.

        50 = neutral.  > 50 = net buying (bullish).  < 50 = net selling (bearish).
        """
        try:
            filings = list_insider_activity(limit=10, symbol=symbol)
        except Exception:
            return 50.0
        if not filings:
            return 50.0
        purchases = sum(1 for f in filings if (f.get("transaction_type") or "").lower() == "purchase")
        sales = sum(1 for f in filings if (f.get("transaction_type") or "").lower() == "sale")
        total = purchases + sales
        if total == 0:
            return 50.0
        return round(50.0 + (purchases - sales) / total * 50.0, 1)

    def scout_rebuild(self) -> None:
        """Saturday 8 AM ET — NexoSignal Scout rebuilds the watchlist from FMP."""
        self._log("NexoSignal Scout watchlist rebuild started", event_type="schedule_job")
        entries = rebuild_watchlist(config.FMP_API_KEY, config.WATCHLIST_SIZE)
        if not entries:
            self._log("NexoSignal Scout: no entries returned (FMP_API_KEY may be unset)", "warning")
            return
        for entry in entries:
            try:
                sym = entry["symbol"]
                score_insider = self._compute_insider_score(sym)
                upsert_watchlist_entry(
                    symbol=sym,
                    composite_score=entry["composite_score"],
                    category=entry["category"],
                    rank_position=entry["rank_position"],
                    score_growth=entry.get("score_growth", 0.0),
                    score_value=entry.get("score_value", 0.0),
                    score_yield=entry.get("score_yield", 0.0),
                    score_sentiment=entry.get("score_sentiment", 0.0),
                    score_insider=score_insider,
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
        """Sunday 8 AM ET — NexoSignal Lens fetches macro regime from FRED."""
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
        """Weekday 7 AM ET — sends pre-market Telegram intelligence briefing."""
        self._log("NexoSignal pre-market briefing job fired", event_type="schedule_job")
        regime = detect_market_regime(config.FRED_API_KEY)
        picks = self._build_daily_top_picks()
        watchlist = list_watchlist(10)
        insiders = list_insider_activity(5)
        self._log(
            f"NexoSignal pre-market: regime={regime['regime']} picks={len(picks)} insiders={len(insiders)}",
            event_type="premarket_briefing",
        )
        self.notifier.send_premarket_briefing(
            picks=picks,
            regime=regime,
            macro=None,
            insiders=insiders,
        )

    def insider_parse(self) -> None:
        """Weekday 6 PM ET — NexoSignal Lens parses SEC EDGAR Form 4 filings."""
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
