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


Signal = Literal["buy", "sell", "hold"]


@dataclass(frozen=True)
class IndicatorSnapshot:
    close: float
    sma_fast: float
    sma_slow: float
    rsi: float
    vwap: float
    volume_ratio: float
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
class AlphaCoreCandidate:
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


@dataclass
class AlphaCoreModel:
    means: dict[str, float]
    weights: dict[str, float]
    xgb_model: Any | None = None
    feature_keys: tuple[str, ...] = ("rsi", "vwap_deviation", "sma_slope_delta", "atr", "volume_z")

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


async def run_alphacore_pipeline(
    snapshot: dict[str, dict],
    headers: dict[str, str],
    data_base: str,
    emit_update=None,
    max_candidates: int = 5,
) -> list[AlphaCoreCandidate]:
    """NexoSignal AlphaCore async three-stage pipeline."""
    stage1 = filter_liquid_stocks(snapshot)
    stage2: list[dict] = []
    for symbol, item in stage1.items():
        bars = snapshot_bars(item)
        if len(bars) < 50:
            continue
        confluence = compute_confluence_score(bars)
        imbalance = compute_order_book_imbalance(item)
        if emit_update:
            emit_update({
                "symbol": symbol,
                "confluence_score": round(confluence, 2),
                "order_book_imbalance": round(imbalance, 4),
            })
        if confluence >= 70:
            stage2.append({
                "symbol": symbol,
                "snapshot": item,
                "bars": bars,
                "confluence_score": confluence,
                "order_book_imbalance": imbalance,
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

    return round((rsi_score * 0.35) + (vwap_score * 0.35) + (sma_score * 0.30), 2)


async def load_or_train_alphacore_model(
    headers: dict[str, str],
    data_base: str,
    symbols: list[str],
) -> AlphaCoreModel:
    model_path = config.ALPHACORE_MODEL_PATH
    if os.path.exists(model_path) and time.time() - os.path.getmtime(model_path) < 24 * 60 * 60:
        with open(model_path, "rb") as fh:
            return pickle.load(fh)

    model = await train_alphacore_model(headers, data_base, symbols)
    with open(model_path, "wb") as fh:
        pickle.dump(model, fh)
    return model


async def train_alphacore_model(headers: dict[str, str], data_base: str, symbols: list[str]) -> AlphaCoreModel:
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

    keys = ["rsi", "vwap_deviation", "sma_slope_delta", "atr", "volume_z"]
    if not rows:
        return AlphaCoreModel(means={k: 0.0 for k in keys}, weights=default_alphacore_weights())

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
    return AlphaCoreModel(means=means, weights=weights, xgb_model=xgb_model, feature_keys=tuple(keys))


async def rank_alphacore_candidates(
    candidates: list[dict],
    model: AlphaCoreModel,
    headers: dict[str, str],
    data_base: str,
) -> list[AlphaCoreCandidate]:
    bars_by_symbol = await asyncio.gather(
        *[fetch_historical_bars(headers, data_base, c["symbol"], limit=80) for c in candidates],
        return_exceptions=True,
    )
    ranked: list[AlphaCoreCandidate] = []
    for candidate, bars in zip(candidates, bars_by_symbol):
        source_bars = candidate["bars"]
        if not isinstance(bars, Exception) and len(bars) >= 20:
            source_bars = bars
        features = alphacore_features(source_bars)
        probability = model.predict_probability(features)
        price = snapshot_price(candidate["snapshot"])
        ranked.append(
            AlphaCoreCandidate(
                symbol=candidate["symbol"],
                price=round(price, 4),
                volume=snapshot_volume(candidate["snapshot"]),
                confluence_score=round(candidate["confluence_score"], 2),
                order_book_imbalance=round(candidate["order_book_imbalance"], 4),
                probability=round(probability, 4),
                atr=round(features["atr"], 4),
                vwap=round(compute_vwap(source_bars), 4),
                rsi=round(features["rsi"], 2),
                sma_slope_delta=round(features["sma_slope_delta"], 4),
            )
        )
    return sorted(ranked, key=lambda c: (c.probability, c.confluence_score), reverse=True)


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
    rsi = compute_rsi(closes)
    vwap = compute_vwap(bars)
    avg_volume = mean(volumes[-20:]) if volumes else 0
    volume_ratio = (volumes[-1] / avg_volume) if avg_volume > 0 else 0
    candle_range = max(high - low, 0.000001)
    candle_body_pct = abs(close - open_) / candle_range
    candle_direction = "bullish" if close > open_ else "bearish" if close < open_ else "neutral"

    if sma_fast > sma_slow and close >= sma_fast:
        trend = "up"
    elif sma_fast < sma_slow and close <= sma_fast:
        trend = "down"
    else:
        trend = "mixed"

    return IndicatorSnapshot(
        close=close,
        sma_fast=round(sma_fast, 4),
        sma_slow=round(sma_slow, 4),
        rsi=round(rsi, 2),
        vwap=round(vwap, 4),
        volume_ratio=round(volume_ratio, 3),
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
