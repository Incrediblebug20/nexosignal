"""
Signal intelligence engine.

This module turns OHLCV bars and a base strategy signal into a deterministic
analysis record. It is intentionally non-LLM and auditable: AI/news providers
can be added later as inputs, but orders should still pass through this scoring
layer and the existing risk gates.
"""

import asyncio
import math
import os
import pickle
import time
from dataclasses import dataclass
from typing import Any, Literal

import aiohttp

from . import config
from .strategy import asset_type_for_symbol


Signal = Literal["buy", "sell", "hold"]
ALPHACORE_FEATURE_KEYS = (
    "rsi",
    "vwap_deviation",
    "sma_slope_delta",
    "atr",
    "volume_z",
    "ema_trend_delta",
    "macd_histogram",
)


@dataclass(frozen=True)
class IndicatorSnapshot:
    close: float
    sma_fast: float
    sma_slow: float
    ema20: float
    ema50: float
    rsi: float
    macd: float
    macd_signal: float
    macd_histogram: float
    vwap: float
    atr: float
    volume_ratio: float
    volume_z: float
    candle_body_pct: float
    candle_direction: str
    trend: str


@dataclass(frozen=True)
class SignalDecision:
    symbol: str
    base_signal: Signal
    final_signal: Signal
    confidence: float
    approved: bool
    reason: str
    indicators: IndicatorSnapshot


@dataclass(frozen=True)
class StrategySignal:
    strategy_name: str
    direction: Signal
    confidence_score: float
    confluence_score: float
    entry_price: float
    target_stop_loss: float
    target_take_profit: float
    risk_reward_ratio: float
    expected_r_multiple: float
    expected_value_score: float
    reason: str
    blocked_reason: str | None = None


@dataclass(frozen=True)
class RiskRewardAnalysis:
    symbol: str
    asset_type: str
    direction: Signal
    entry: float
    invalidation_level: float
    stop_loss: float
    take_profit: float
    break_even_trigger: float
    atr: float
    risk_per_share: float
    reward_per_share: float
    risk_reward_ratio: float
    dollar_risk: float
    notional_exposure: float
    buying_power_required: float
    position_size: float
    expected_r_multiple: float
    expected_value_score: float
    drawdown_exposure: float
    volatility_adjusted_confidence: float
    liquidity_adjusted_confidence: float
    trade_allowed: bool
    blocked_reason: str | None


@dataclass(frozen=True)
class DailyPrediction:
    rank: int
    symbol: str
    asset_type: str
    current_price: float
    predicted_direction: Signal
    confidence_score: float
    confluence_score: float
    strategy_match: str
    expected_entry: float
    target_stop_loss: float
    target_take_profit: float
    risk_reward_ratio: float
    expected_r_multiple: float
    expected_value_score: float
    invalidation_level: float
    reason: str
    risk_warning: str
    trade_allowed: bool
    blocked_reason: str | None


@dataclass(frozen=True)
class AlphaPick:
    symbol: str
    asset_type: str
    price: float
    score: float
    confluence_score: float
    confidence_score: float
    atr: float
    vwap_status: str
    relative_volume: float
    direction: Signal
    action_recommendation: str
    rejection_reason: str | None


@dataclass(frozen=True)
class NexoSignalAlphaCoreCandidate:
    symbol: str
    price: float
    volume: float
    confluence_score: float
    order_book_imbalance: float
    probability: float
    atr: float
    vwap: float
    rsi: float
    sma_slope_delta: float
    ema_trend_delta: float
    macd_histogram: float
    liquidity_score: float
    spread_bps: float
    slippage_estimate_bps: float


@dataclass(frozen=True)
class NexoSignalLiquiditySnapshot:
    order_book_imbalance: float
    spread_bps: float
    slippage_estimate_bps: float
    liquidity_score: float


@dataclass
class NexoSignalAlphaCoreModel:
    means: dict[str, float]
    weights: dict[str, float]
    xgb_model: Any | None = None
    feature_keys: tuple[str, ...] = ALPHACORE_FEATURE_KEYS

    def predict_probability(self, features: dict[str, float]) -> float:
        if self.xgb_model is not None:
            matrix = [[features.get(key, 0.0) for key in self.feature_keys]]
            return float(self.xgb_model.predict_proba(matrix)[0][1])
        score = 0.0
        for key, weight in self.weights.items():
            center = self.means.get(key, 0.0)
            score += weight * (features.get(key, 0.0) - center)
        return 1 / (1 + math.exp(-max(-6, min(6, score))))


def analyze_signal(
    symbol: str,
    bars: list[dict],
    base_signal: Signal,
    min_confidence: float = 70,
) -> SignalDecision:
    indicators = compute_indicators(bars)
    confidence, reasons = score_signal(base_signal, indicators)
    approved = base_signal != "hold" and confidence >= min_confidence
    final_signal: Signal = base_signal if approved else "hold"

    if base_signal == "hold":
        reasons.insert(0, "base strategy is hold")
    elif not approved:
        reasons.insert(0, f"confidence below execution threshold ({confidence:.0f} < {min_confidence:.0f})")
    else:
        reasons.insert(0, f"{base_signal} approved by signal engine")

    return SignalDecision(
        symbol=symbol.upper(),
        base_signal=base_signal,
        final_signal=final_signal,
        confidence=round(confidence, 2),
        approved=approved,
        reason="; ".join(reasons),
        indicators=indicators,
    )


def evaluate_strategy_suite(symbol: str, bars: list[dict]) -> list[StrategySignal]:
    """Run deterministic strategy family and return structured strategy outputs."""
    indicators = compute_indicators(bars)
    return [
        _trend_momentum_signal(indicators),
        _mean_reversion_signal(indicators),
        _breakout_signal(bars, indicators),
        _volatility_expansion_signal(bars, indicators),
        _institutional_risk_reward_signal(symbol, bars, indicators),
    ]


def select_best_strategy_signal(symbol: str, bars: list[dict]) -> StrategySignal:
    candidates = evaluate_strategy_suite(symbol, bars)
    return sorted(
        candidates,
        key=lambda item: (item.blocked_reason is None, item.expected_value_score, item.confluence_score),
        reverse=True,
    )[0]


def analyze_risk_reward(
    symbol: str,
    bars: list[dict],
    direction: Signal = "buy",
    max_risk_per_trade: float = 100.0,
    buying_power: float | None = None,
    market_open: bool = True,
    circuit_breaker_active: bool = False,
    stale_data: bool = False,
) -> RiskRewardAnalysis:
    indicators = compute_indicators(bars)
    entry = indicators.close
    atr = indicators.atr
    blocked: list[str] = []
    if direction not in {"buy", "sell"}:
        blocked.append("no directional signal")
    if atr <= 0:
        blocked.append("ATR missing or zero")
    if stale_data:
        blocked.append("market data is stale")
    if not market_open and asset_type_for_symbol(symbol) != "crypto":
        blocked.append("market is closed")
    if circuit_breaker_active:
        blocked.append("circuit breaker active")

    if direction == "sell":
        stop_loss = entry + atr
        take_profit = entry - (5 * atr)
        break_even_trigger = entry - (2 * atr)
        invalidation = stop_loss
        risk_per_share = max(stop_loss - entry, 0.0)
        reward_per_share = max(entry - take_profit, 0.0)
    else:
        stop_loss = entry - atr
        take_profit = entry + (5 * atr)
        break_even_trigger = entry + (2 * atr)
        invalidation = stop_loss
        risk_per_share = max(entry - stop_loss, 0.0)
        reward_per_share = max(take_profit - entry, 0.0)

    rr = reward_per_share / max(risk_per_share, 0.0001)
    if rr + 1e-6 < config.AI_MIN_RISK_REWARD_RATIO:
        blocked.append(f"risk reward below {config.AI_MIN_RISK_REWARD_RATIO:.1f}:1")

    size_by_risk = math.floor(max_risk_per_trade / max(risk_per_share, 0.0001)) if risk_per_share > 0 else 0
    if buying_power is not None:
        size_by_cash = math.floor(max(buying_power, 0.0) / max(entry, 0.0001))
        position_size = max(0, min(size_by_risk, size_by_cash))
        if position_size <= 0:
            blocked.append("buying power unavailable")
    else:
        position_size = max(0, size_by_risk)

    notional = position_size * entry
    dollar_risk = position_size * risk_per_share
    expected_r = (rr * (indicators.volume_ratio / max(indicators.volume_ratio + 1.0, 1.0))) - 1.0
    ev = max(0.0, min(100.0, 50.0 + expected_r * 10.0))
    volatility_adjusted = max(0.0, min(100.0, 100.0 - (atr / max(entry, 0.0001)) * 900.0))
    liquidity_adjusted = max(0.0, min(100.0, 55.0 + indicators.volume_ratio * 20.0 - abs(indicators.volume_z) * 2.0))

    return RiskRewardAnalysis(
        symbol=symbol.upper(),
        asset_type=asset_type_for_symbol(symbol),
        direction=direction,
        entry=round(entry, 4),
        invalidation_level=round(invalidation, 4),
        stop_loss=round(stop_loss, 4),
        take_profit=round(take_profit, 4),
        break_even_trigger=round(break_even_trigger, 4),
        atr=round(atr, 4),
        risk_per_share=round(risk_per_share, 4),
        reward_per_share=round(reward_per_share, 4),
        risk_reward_ratio=round(rr, 2),
        dollar_risk=round(dollar_risk, 2),
        notional_exposure=round(notional, 2),
        buying_power_required=round(notional, 2),
        position_size=float(position_size),
        expected_r_multiple=round(expected_r, 2),
        expected_value_score=round(ev, 2),
        drawdown_exposure=round((dollar_risk / max(notional, 0.0001)) if notional else 0.0, 4),
        volatility_adjusted_confidence=round(volatility_adjusted, 2),
        liquidity_adjusted_confidence=round(liquidity_adjusted, 2),
        trade_allowed=not blocked,
        blocked_reason="; ".join(dict.fromkeys(blocked)) if blocked else None,
    )


def build_ranked_predictions(
    symbol_bars: dict[str, list[dict]],
    *,
    top_n: int = 3,
    market_open: bool = True,
    circuit_breaker_active: bool = False,
) -> list[DailyPrediction]:
    ranked: list[tuple[float, str, StrategySignal, RiskRewardAnalysis]] = []
    for symbol, bars in symbol_bars.items():
        if len(bars) < 20:
            continue
        best = select_best_strategy_signal(symbol, bars)
        rr = analyze_risk_reward(
            symbol,
            bars,
            best.direction,
            market_open=market_open,
            circuit_breaker_active=circuit_breaker_active,
        )
        indicators = compute_indicators(bars)
        score = (
            best.confluence_score * 0.38
            + best.confidence_score * 0.22
            + min(100.0, indicators.volume_ratio * 45.0) * 0.15
            + min(100.0, rr.risk_reward_ratio * 12.5) * 0.15
            + rr.expected_value_score * 0.10
        )
        if best.direction == "hold":
            score *= 0.55
        ranked.append((score, symbol, best, rr))

    output: list[DailyPrediction] = []
    for rank, (score, symbol, best, rr) in enumerate(sorted(ranked, key=lambda row: row[0], reverse=True)[:top_n], start=1):
        blocked = rr.blocked_reason or best.blocked_reason
        allowed = score >= config.SIGNAL_MIN_CONFIDENCE and rr.trade_allowed and best.blocked_reason is None
        output.append(
            DailyPrediction(
                rank=rank,
                symbol=symbol.upper(),
                asset_type=asset_type_for_symbol(symbol),
                current_price=rr.entry,
                predicted_direction=best.direction,
                confidence_score=round(best.confidence_score, 2),
                confluence_score=round(max(best.confluence_score, score), 2),
                strategy_match=best.strategy_name,
                expected_entry=rr.entry,
                target_stop_loss=rr.stop_loss,
                target_take_profit=rr.take_profit,
                risk_reward_ratio=rr.risk_reward_ratio,
                expected_r_multiple=rr.expected_r_multiple,
                expected_value_score=round(max(best.expected_value_score, rr.expected_value_score), 2),
                invalidation_level=rr.invalidation_level,
                reason=best.reason,
                risk_warning="Probabilistic ranking only. Not a guaranteed prediction.",
                trade_allowed=allowed,
                blocked_reason=None if allowed else (blocked or "score below autonomous threshold"),
            )
        )
    return output


def build_alpha_picks(symbol_bars: dict[str, list[dict]], top_n: int = 5) -> list[AlphaPick]:
    picks: list[AlphaPick] = []
    for symbol, bars in symbol_bars.items():
        if len(bars) < 20:
            continue
        indicators = compute_indicators(bars)
        best = select_best_strategy_signal(symbol, bars)
        rr = analyze_risk_reward(symbol, bars, best.direction)
        above_vwap = indicators.close >= indicators.vwap
        score = round((best.confluence_score * 0.55) + (best.confidence_score * 0.25) + (rr.expected_value_score * 0.20), 2)
        blocked = best.blocked_reason or rr.blocked_reason
        if blocked:
            action = "blocked"
        elif score >= config.SIGNAL_MIN_CONFIDENCE and rr.risk_reward_ratio >= config.AI_MIN_RISK_REWARD_RATIO:
            action = "paper trade"
        else:
            action = "watch"
        picks.append(
            AlphaPick(
                symbol=symbol.upper(),
                asset_type=asset_type_for_symbol(symbol),
                price=round(indicators.close, 4),
                score=score,
                confluence_score=round(best.confluence_score, 2),
                confidence_score=round(best.confidence_score, 2),
                atr=round(indicators.atr, 4),
                vwap_status="above" if above_vwap else "below",
                relative_volume=round(indicators.volume_ratio, 2),
                direction=best.direction,
                action_recommendation=action,
                rejection_reason=blocked,
            )
        )
    return sorted(picks, key=lambda item: item.score, reverse=True)[:top_n]


def _strategy_result(
    name: str,
    direction: Signal,
    confidence: float,
    confluence: float,
    indicators: IndicatorSnapshot,
    reason: str,
    blocked: str | None = None,
) -> StrategySignal:
    rr = analyze_risk_reward_from_indicators(indicators, direction)
    if blocked is None and direction == "hold":
        blocked = "no executable directional edge"
    if blocked is None and rr["risk_reward_ratio"] + 1e-6 < config.AI_MIN_RISK_REWARD_RATIO:
        blocked = f"risk reward below {config.AI_MIN_RISK_REWARD_RATIO:.1f}:1"
    return StrategySignal(
        strategy_name=name,
        direction=direction,
        confidence_score=round(max(0.0, min(100.0, confidence)), 2),
        confluence_score=round(max(0.0, min(100.0, confluence)), 2),
        entry_price=rr["entry"],
        target_stop_loss=rr["stop_loss"],
        target_take_profit=rr["take_profit"],
        risk_reward_ratio=rr["risk_reward_ratio"],
        expected_r_multiple=rr["expected_r_multiple"],
        expected_value_score=rr["expected_value_score"],
        reason=reason,
        blocked_reason=blocked,
    )


def analyze_risk_reward_from_indicators(indicators: IndicatorSnapshot, direction: Signal) -> dict[str, float]:
    entry = indicators.close
    atr = indicators.atr
    if atr <= 0:
        return {
            "entry": round(entry, 4),
            "stop_loss": round(entry, 4),
            "take_profit": round(entry, 4),
            "risk_reward_ratio": 0.0,
            "expected_r_multiple": -1.0,
            "expected_value_score": 0.0,
        }
    if direction == "sell":
        stop = entry + atr
        target = entry - (5 * atr)
        risk = stop - entry
        reward = entry - target
    else:
        stop = entry - atr
        target = entry + (5 * atr)
        risk = entry - stop
        reward = target - entry
    rr = reward / max(risk, 0.0001) if direction in {"buy", "sell"} else 0.0
    probability = max(0.05, min(0.95, (indicators.volume_ratio / 3.0 + (50.0 - abs(indicators.rsi - 50.0)) / 50.0) / 2.0))
    expected_r = (probability * rr) - (1.0 - probability)
    return {
        "entry": round(entry, 4),
        "stop_loss": round(stop, 4),
        "take_profit": round(target, 4),
        "risk_reward_ratio": round(rr, 2),
        "expected_r_multiple": round(expected_r, 2),
        "expected_value_score": round(max(0.0, min(100.0, 50.0 + expected_r * 10.0)), 2),
    }


def _trend_momentum_signal(i: IndicatorSnapshot) -> StrategySignal:
    score = 45.0
    reasons: list[str] = []
    direction: Signal = "hold"
    if i.close > i.sma_slow and i.ema20 > i.ema50 and i.macd_histogram > 0:
        direction = "buy"
        score += 32
        reasons.append("price above slow SMA with EMA and MACD confirmation")
    elif i.close < i.sma_slow and i.ema20 < i.ema50 and i.macd_histogram < 0:
        direction = "sell"
        score += 30
        reasons.append("price below slow SMA with bearish EMA and MACD confirmation")
    if i.volume_ratio >= 1.2:
        score += 10
        reasons.append("relative volume confirms momentum")
    if 42 <= i.rsi <= 68:
        score += 8
        reasons.append("RSI supports trend continuation")
    return _strategy_result("Trend Momentum Strategy", direction, score, score, i, "; ".join(reasons) or "trend not aligned")


def _mean_reversion_signal(i: IndicatorSnapshot) -> StrategySignal:
    vwap_dev = (i.close - i.vwap) / max(i.vwap, 0.0001)
    score = 50.0
    direction: Signal = "hold"
    reason = "price is not stretched enough for mean reversion"
    if i.rsi <= 32 and vwap_dev < -0.01:
        direction = "buy"
        score += min(35.0, abs(vwap_dev) * 1200.0)
        reason = "oversold RSI with price below VWAP"
    elif i.rsi >= 72 and vwap_dev > 0.01:
        direction = "sell"
        score += min(35.0, abs(vwap_dev) * 1200.0)
        reason = "overbought RSI with price above VWAP"
    return _strategy_result("Mean Reversion Strategy", direction, score, score, i, reason)


def _breakout_signal(bars: list[dict], i: IndicatorSnapshot) -> StrategySignal:
    highs = [float(b.get("h", b["c"])) for b in bars[-21:-1]]
    lows = [float(b.get("l", b["c"])) for b in bars[-21:-1]]
    resistance = max(highs) if highs else i.close
    support = min(lows) if lows else i.close
    direction: Signal = "hold"
    score = 48.0
    reason = "no confirmed support or resistance break"
    if i.close > resistance and i.volume_ratio >= 1.1:
        direction = "buy"
        score = 76.0 + min(18.0, (i.volume_ratio - 1.0) * 10.0)
        reason = "price broke above 20-bar resistance with volume"
    elif i.close < support and i.volume_ratio >= 1.1:
        direction = "sell"
        score = 74.0 + min(18.0, (i.volume_ratio - 1.0) * 10.0)
        reason = "price broke below 20-bar support with volume"
    return _strategy_result("Breakout Strategy", direction, score, score, i, reason)


def _volatility_expansion_signal(bars: list[dict], i: IndicatorSnapshot) -> StrategySignal:
    atr_now = i.atr
    prev_atrs = []
    for idx in range(16, len(bars) - 1):
        prev_atrs.append(compute_atr(bars[max(0, idx - 15):idx + 1]))
    atr_avg = mean(prev_atrs[-20:]) if prev_atrs else atr_now
    expanding = atr_now > atr_avg * 1.15 if atr_avg else False
    direction: Signal = "hold"
    score = 45.0
    reason = "volatility is not expanding"
    if expanding and i.candle_direction == "bullish" and i.close >= i.vwap:
        direction = "buy"
        score = 72.0 + min(18.0, i.candle_body_pct * 20.0)
        reason = "ATR expansion with bullish candle above VWAP"
    elif expanding and i.candle_direction == "bearish" and i.close <= i.vwap:
        direction = "sell"
        score = 72.0 + min(18.0, i.candle_body_pct * 20.0)
        reason = "ATR expansion with bearish candle below VWAP"
    return _strategy_result("Volatility Expansion Strategy", direction, score, score, i, reason)


def _institutional_risk_reward_signal(symbol: str, bars: list[dict], i: IndicatorSnapshot) -> StrategySignal:
    direction: Signal = "buy" if i.trend == "up" else "sell" if i.trend == "down" else "hold"
    rr = analyze_risk_reward(symbol, bars, direction)
    score = min(100.0, (rr.risk_reward_ratio * 11.0) + (rr.liquidity_adjusted_confidence * 0.25))
    reason = f"ATR-derived 5:1 setup with {i.trend} market structure"
    return _strategy_result(
        "Institutional Risk/Reward Strategy",
        direction,
        score,
        score,
        i,
        reason,
        rr.blocked_reason,
    )


async def run_alphacore_pipeline(
    snapshot: dict[str, dict],
    headers: dict[str, str],
    data_base: str,
    emit_update=None,
    max_candidates: int = 5,
) -> list[NexoSignalAlphaCoreCandidate]:
    """NexoSignal AlphaCore async three-stage pipeline."""
    stage1 = filter_liquid_stocks(snapshot)
    stage2: list[dict] = []
    for symbol, item in stage1.items():
        bars = snapshot_bars(item)
        if len(bars) < 50:
            continue
        confluence = compute_confluence_score(bars)
        liquidity = compute_liquidity_snapshot(item, bars)
        if emit_update:
            emit_update({
                "symbol": symbol,
                "confluence_score": round(confluence, 2),
                "order_book_imbalance": round(liquidity.order_book_imbalance, 4),
                "liquidity_score": round(liquidity.liquidity_score, 2),
                "spread_bps": round(liquidity.spread_bps, 2),
            })
        if confluence >= 70 and liquidity.liquidity_score >= 55:
            stage2.append({
                "symbol": symbol,
                "snapshot": item,
                "bars": bars,
                "confluence_score": confluence,
                "liquidity": liquidity,
                "order_book_imbalance": liquidity.order_book_imbalance,
            })

    if not stage2:
        return []

    model = await load_or_train_alphacore_model(headers, data_base, [c["symbol"] for c in stage2[:50]])
    ranked = await rank_alphacore_candidates(stage2, model, headers, data_base)
    return ranked[:max_candidates]


def filter_liquid_stocks(snapshot: dict[str, dict]) -> dict[str, dict]:
    """NexoSignal AlphaCore Stage 1: liquid plain equities only."""
    passed: dict[str, dict] = {}
    for symbol, item in snapshot.items():
        symbol = symbol.upper()
        if "." in symbol or "/" in symbol:
            continue
        price = snapshot_price(item)
        volume = snapshot_volume(item)
        if price > 5 and volume > 1_500_000:
            passed[symbol] = item
    return passed


def compute_confluence_score(bars: list[dict]) -> float:
    """NexoSignal AlphaCore Stage 2 weighted confluence score."""
    closes = [float(b["c"]) for b in bars]
    if len(closes) < 50:
        return 0.0
    current = closes[-1]
    rsi = compute_rsi(closes)
    vwap = compute_vwap(bars)
    sma20 = mean(closes[-20:])
    sma50 = mean(closes[-50:])
    ema20 = compute_ema(closes, 20)
    ema50 = compute_ema(closes, 50)
    macd, macd_signal, macd_hist = compute_macd(closes)
    prev_sma20 = mean(closes[-25:-5]) if len(closes) >= 55 else sma20
    prev_sma50 = mean(closes[-55:-5]) if len(closes) >= 55 else sma50

    rsi_score = max(0.0, min(100.0, (rsi - 30) / 40 * 100))
    if 40 <= rsi <= 60:
        rsi_score = 50 + (rsi - 50) * 1.5

    vwap_deviation = abs(current - vwap) / max(vwap, 0.0001)
    if vwap_deviation <= 0.005:
        vwap_score = 100.0
    elif vwap_deviation >= 0.02:
        vwap_score = 0.0
    else:
        vwap_score = 100 - ((vwap_deviation - 0.005) / 0.015 * 100)

    slope_positive = (sma20 - prev_sma20) > (sma50 - prev_sma50)
    if sma20 > sma50 and slope_positive:
        sma_score = 100.0
    elif sma20 > sma50:
        sma_score = 70.0
    elif sma20 < sma50:
        sma_score = 0.0
    else:
        sma_score = 50.0

    ema_score = 100.0 if ema20 > ema50 and current > ema20 else 45.0 if ema20 > ema50 else 0.0
    macd_score = 100.0 if macd_hist > 0 and macd >= macd_signal else 40.0 if macd_hist > -0.02 else 0.0

    return round(
        (rsi_score * 0.25)
        + (vwap_score * 0.25)
        + (sma_score * 0.20)
        + (ema_score * 0.15)
        + (macd_score * 0.15),
        2,
    )


async def load_or_train_alphacore_model(
    headers: dict[str, str],
    data_base: str,
    symbols: list[str],
) -> NexoSignalAlphaCoreModel:
    model_path = config.ALPHACORE_MODEL_PATH
    if os.path.exists(model_path) and time.time() - os.path.getmtime(model_path) < 24 * 60 * 60:
        with open(model_path, "rb") as fh:
            return pickle.load(fh)

    model = await train_alphacore_model(headers, data_base, symbols)
    with open(model_path, "wb") as fh:
        pickle.dump(model, fh)
    return model


async def train_alphacore_model(headers: dict[str, str], data_base: str, symbols: list[str]) -> NexoSignalAlphaCoreModel:
    """NexoSignal AlphaCore Stage 3 training with optional XGBoost-compatible fallback."""
    datasets = await asyncio.gather(
        *[fetch_historical_bars(headers, data_base, symbol, limit=120) for symbol in symbols[:25]],
        return_exceptions=True,
    )
    rows: list[dict[str, float]] = []
    labels: list[int] = []
    for bars in datasets:
        if isinstance(bars, Exception) or len(bars) < 35:
            continue
        for i in range(30, len(bars) - 1):
            window = bars[max(0, i - 30):i + 1]
            features = alphacore_features(window)
            rows.append(features)
            labels.append(1 if float(bars[i + 1]["c"]) > float(bars[i]["c"]) else 0)

    keys = list(ALPHACORE_FEATURE_KEYS)
    if not rows:
        return NexoSignalAlphaCoreModel(means={k: 0.0 for k in keys}, weights=default_alphacore_weights())

    try:
        from xgboost import XGBClassifier

        clf = XGBClassifier(n_estimators=30, max_depth=3, learning_rate=0.1, eval_metric="logloss")
        matrix = [[row[k] for k in keys] for row in rows]
        clf.fit(matrix, labels)
        importances = clf.feature_importances_
        weights = {key: float(importances[idx]) for idx, key in enumerate(keys)}
        xgb_model = clf
    except Exception:
        weights = default_alphacore_weights()
        xgb_model = None

    means = {key: mean([row[key] for row in rows]) for key in keys}
    return NexoSignalAlphaCoreModel(means=means, weights=weights, xgb_model=xgb_model, feature_keys=tuple(keys))


async def rank_alphacore_candidates(
    candidates: list[dict],
    model: NexoSignalAlphaCoreModel,
    headers: dict[str, str],
    data_base: str,
) -> list[NexoSignalAlphaCoreCandidate]:
    bars_by_symbol = await asyncio.gather(
        *[fetch_historical_bars(headers, data_base, c["symbol"], limit=80) for c in candidates],
        return_exceptions=True,
    )
    ranked: list[NexoSignalAlphaCoreCandidate] = []
    for candidate, bars in zip(candidates, bars_by_symbol):
        source_bars = candidate["bars"]
        if not isinstance(bars, Exception) and len(bars) >= 20:
            source_bars = bars
        features = alphacore_features(source_bars)
        probability = model.predict_probability(features)
        price = snapshot_price(candidate["snapshot"])
        liquidity: NexoSignalLiquiditySnapshot = candidate.get("liquidity") or compute_liquidity_snapshot(candidate["snapshot"], source_bars)
        ranked.append(
            NexoSignalAlphaCoreCandidate(
                symbol=candidate["symbol"],
                price=round(price, 4),
                volume=snapshot_volume(candidate["snapshot"]),
                confluence_score=round(candidate["confluence_score"], 2),
                order_book_imbalance=round(liquidity.order_book_imbalance, 4),
                probability=round(probability, 4),
                atr=round(features["atr"], 4),
                vwap=round(compute_vwap(source_bars), 4),
                rsi=round(features["rsi"], 2),
                sma_slope_delta=round(features["sma_slope_delta"], 4),
                ema_trend_delta=round(features["ema_trend_delta"], 4),
                macd_histogram=round(features["macd_histogram"], 4),
                liquidity_score=round(liquidity.liquidity_score, 2),
                spread_bps=round(liquidity.spread_bps, 2),
                slippage_estimate_bps=round(liquidity.slippage_estimate_bps, 2),
            )
        )
    return sorted(ranked, key=lambda c: (c.probability, c.confluence_score), reverse=True)


AlphaCoreCandidate = NexoSignalAlphaCoreCandidate
AlphaCoreModel = NexoSignalAlphaCoreModel


async def fetch_historical_bars(headers: dict[str, str], data_base: str, symbol: str, limit: int = 120) -> list[dict]:
    params = {"timeframe": "1Day", "limit": limit, "feed": "iex"}
    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.get(f"{data_base}/v2/stocks/{symbol}/bars", params=params, timeout=20) as resp:
            if resp.status >= 400:
                return []
            data = await resp.json()
            return data.get("bars", [])


def alphacore_features(bars: list[dict]) -> dict[str, float]:
    closes = [float(b["c"]) for b in bars]
    volumes = [float(b.get("v", 0)) for b in bars]
    vwap = compute_vwap(bars)
    current = closes[-1]
    sma20 = mean(closes[-20:]) if len(closes) >= 20 else mean(closes)
    sma50 = mean(closes[-50:]) if len(closes) >= 50 else mean(closes)
    ema20 = compute_ema(closes, 20)
    ema50 = compute_ema(closes, 50)
    _, _, macd_hist = compute_macd(closes)
    prev_sma20 = mean(closes[-25:-5]) if len(closes) >= 55 else sma20
    prev_sma50 = mean(closes[-55:-5]) if len(closes) >= 55 else sma50
    volume_avg = mean(volumes[-30:]) if volumes else 0.0
    variance = mean([(v - volume_avg) ** 2 for v in volumes[-30:]]) if volumes else 0.0
    volume_std = math.sqrt(variance) if variance > 0 else 1.0
    return {
        "rsi": compute_rsi(closes),
        "vwap_deviation": (current - vwap) / max(vwap, 0.0001),
        "sma_slope_delta": (sma20 - prev_sma20) - (sma50 - prev_sma50),
        "atr": compute_atr(bars),
        "volume_z": ((volumes[-1] - volume_avg) / volume_std) if volumes else 0.0,
        "ema_trend_delta": ema20 - ema50,
        "macd_histogram": macd_hist,
    }


def compute_atr(bars: list[dict], period: int = 14) -> float:
    if len(bars) < 2:
        return 0.0
    trs = []
    for idx in range(1, len(bars)):
        high = float(bars[idx].get("h", bars[idx]["c"]))
        low = float(bars[idx].get("l", bars[idx]["c"]))
        prev_close = float(bars[idx - 1]["c"])
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    return mean(trs[-period:])


def default_alphacore_weights() -> dict[str, float]:
    return {
        "rsi": 0.010,
        "vwap_deviation": -10.0,
        "sma_slope_delta": 0.50,
        "atr": -0.02,
        "volume_z": 0.15,
        "ema_trend_delta": 0.25,
        "macd_histogram": 0.30,
    }


def snapshot_price(item: dict) -> float:
    trade = item.get("latestTrade") or item.get("trade") or {}
    minute_bar = item.get("minuteBar") or {}
    daily_bar = item.get("dailyBar") or {}
    return float(trade.get("p") or minute_bar.get("c") or daily_bar.get("c") or 0)


def snapshot_volume(item: dict) -> float:
    daily_bar = item.get("dailyBar") or {}
    return float(daily_bar.get("v") or 0)


def snapshot_bars(item: dict) -> list[dict]:
    bars = []
    for key in ("dailyBar", "prevDailyBar", "minuteBar"):
        bar = item.get(key)
        if bar:
            bars.append(bar)
    if len(bars) >= 2:
        return bars * 25
    return bars


def compute_order_book_imbalance(item: dict) -> float:
    quote = item.get("latestQuote") or item.get("quote") or {}
    bid_size = float(quote.get("bs") or 0)
    ask_size = float(quote.get("as") or 0)
    total = bid_size + ask_size
    if total <= 0:
        return 0.0
    return (bid_size - ask_size) / total


def compute_liquidity_snapshot(item: dict, bars: list[dict] | None = None) -> NexoSignalLiquiditySnapshot:
    quote = item.get("latestQuote") or item.get("quote") or {}
    bid = float(quote.get("bp") or quote.get("bid_price") or 0)
    ask = float(quote.get("ap") or quote.get("ask_price") or 0)
    price = snapshot_price(item) or ((bid + ask) / 2 if bid and ask else 0.0)
    spread = max(ask - bid, 0.0) if bid > 0 and ask > 0 else 0.0
    spread_bps = (spread / max(price, 0.0001)) * 10_000 if price else 999.0
    imbalance = compute_order_book_imbalance(item)
    volume = snapshot_volume(item)
    avg_volume = volume
    if bars:
        vols = [float(b.get("v", 0)) for b in bars if b.get("v") is not None]
        avg_volume = mean(vols[-20:]) if vols else volume
    volume_ratio = volume / max(avg_volume, 1.0)
    spread_penalty = min(45.0, spread_bps * 2.0)
    imbalance_bonus = max(-15.0, min(15.0, imbalance * 20.0))
    volume_bonus = max(0.0, min(20.0, (volume_ratio - 1.0) * 10.0))
    liquidity_score = max(0.0, min(100.0, 75.0 - spread_penalty + imbalance_bonus + volume_bonus))
    slippage_estimate_bps = max(0.0, spread_bps / 2.0) + max(0.0, 10.0 - liquidity_score / 10.0)
    return NexoSignalLiquiditySnapshot(
        order_book_imbalance=imbalance,
        spread_bps=spread_bps,
        slippage_estimate_bps=slippage_estimate_bps,
        liquidity_score=liquidity_score,
    )


def compute_indicators(bars: list[dict]) -> IndicatorSnapshot:
    if not bars:
        raise ValueError("bars are required")

    closes = [float(b["c"]) for b in bars]
    volumes = [float(b.get("v", 0)) for b in bars]
    last = bars[-1]
    close = float(last["c"])
    open_ = float(last.get("o", close))
    high = float(last.get("h", close))
    low = float(last.get("l", close))

    sma_fast = mean(closes[-5:])
    sma_slow = mean(closes[-20:]) if len(closes) >= 20 else mean(closes)
    ema20 = compute_ema(closes, 20)
    ema50 = compute_ema(closes, 50)
    rsi = compute_rsi(closes)
    macd, macd_signal, macd_hist = compute_macd(closes)
    vwap = compute_vwap(bars)
    atr = compute_atr(bars)
    avg_volume = mean(volumes[-20:]) if volumes else 0
    volume_ratio = (volumes[-1] / avg_volume) if avg_volume > 0 else 0
    volume_z = compute_volume_z(volumes)
    candle_range = max(high - low, 0.000001)
    candle_body_pct = abs(close - open_) / candle_range
    candle_direction = "bullish" if close > open_ else "bearish" if close < open_ else "neutral"

    if sma_fast > sma_slow and close >= sma_fast and ema20 >= ema50:
        trend = "up"
    elif sma_fast < sma_slow and close <= sma_fast and ema20 <= ema50:
        trend = "down"
    else:
        trend = "mixed"

    return IndicatorSnapshot(
        close=close,
        sma_fast=round(sma_fast, 4),
        sma_slow=round(sma_slow, 4),
        ema20=round(ema20, 4),
        ema50=round(ema50, 4),
        rsi=round(rsi, 2),
        macd=round(macd, 4),
        macd_signal=round(macd_signal, 4),
        macd_histogram=round(macd_hist, 4),
        vwap=round(vwap, 4),
        atr=round(atr, 4),
        volume_ratio=round(volume_ratio, 3),
        volume_z=round(volume_z, 3),
        candle_body_pct=round(candle_body_pct, 3),
        candle_direction=candle_direction,
        trend=trend,
    )


def score_signal(signal: Signal, i: IndicatorSnapshot) -> tuple[float, list[str]]:
    if signal == "hold":
        return 0, []

    score = 50.0
    reasons: list[str] = []

    if signal == "buy":
        if i.trend == "up":
            score += 15
            reasons.append("trend confirms buy")
        if i.close >= i.vwap:
            score += 10
            reasons.append("price above VWAP")
        if i.ema20 >= i.ema50 and i.macd_histogram > 0:
            score += 10
            reasons.append("EMA/MACD momentum confirms buy")
        if 35 <= i.rsi <= 68:
            score += 10
            reasons.append("RSI in constructive range")
        elif i.rsi > 75:
            score -= 20
            reasons.append("RSI overbought")
        if i.candle_direction == "bullish" and i.candle_body_pct >= 0.4:
            score += 10
            reasons.append("bullish candle confirmation")

    if signal == "sell":
        if i.trend == "down":
            score += 15
            reasons.append("trend confirms sell")
        if i.close <= i.vwap:
            score += 10
            reasons.append("price below VWAP")
        if i.ema20 <= i.ema50 and i.macd_histogram < 0:
            score += 10
            reasons.append("EMA/MACD momentum confirms sell")
        if 32 <= i.rsi <= 65:
            score += 10
            reasons.append("RSI supports exit/short bias")
        elif i.rsi < 25:
            score -= 20
            reasons.append("RSI oversold")
        if i.candle_direction == "bearish" and i.candle_body_pct >= 0.4:
            score += 10
            reasons.append("bearish candle confirmation")

    if i.volume_ratio >= 1.2:
        score += 10
        reasons.append("above-average volume")
    elif i.volume_ratio < 0.5:
        score -= 10
        reasons.append("weak volume")

    return max(0, min(100, score)), reasons


def compute_ema(values: list[float], period: int) -> float:
    if not values:
        return 0.0
    window = values[-period:] if len(values) >= period else values
    multiplier = 2 / (len(window) + 1)
    ema = window[0]
    for value in window[1:]:
        ema = (value - ema) * multiplier + ema
    return ema


def compute_macd(closes: list[float]) -> tuple[float, float, float]:
    if not closes:
        return 0.0, 0.0, 0.0
    macd_series: list[float] = []
    for idx in range(len(closes)):
        subset = closes[: idx + 1]
        macd_series.append(compute_ema(subset, 12) - compute_ema(subset, 26))
    macd = macd_series[-1]
    signal = compute_ema(macd_series, 9)
    return macd, signal, macd - signal


def compute_volume_z(volumes: list[float], period: int = 30) -> float:
    if not volumes:
        return 0.0
    window = volumes[-period:]
    avg = mean(window)
    variance = mean([(v - avg) ** 2 for v in window])
    std = math.sqrt(variance) if variance > 0 else 1.0
    return (volumes[-1] - avg) / std


def compute_rsi(closes: list[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [d for d in deltas[-period:] if d > 0]
    losses = [-d for d in deltas[-period:] if d < 0]
    avg_gain = sum(gains) / period if gains else 0
    avg_loss = sum(losses) / period if losses else 0
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)


def compute_vwap(bars: list[dict]) -> float:
    total_volume = sum(float(b.get("v", 0)) for b in bars)
    if total_volume <= 0:
        return float(bars[-1]["c"])
    return sum(
        float(b.get("v", 0)) * (float(b["h"]) + float(b["l"]) + float(b["c"])) / 3
        for b in bars
    ) / total_volume


def mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0
