"""
Pluggable trading strategies. Each strategy receives bars (OHLCV list)
and returns a signal: "buy", "sell", or "hold".

Add your own by subclassing BaseStrategy.
"""

import logging
import requests
from abc import ABC, abstractmethod
from datetime import date, timedelta
from typing import Literal, Optional

from . import config

logger = logging.getLogger("trading_agent.strategy")


Signal = Literal["buy", "sell", "hold"]
AssetType = Literal["stock", "etf", "crypto"]


SUPPORTED_CRYPTO_SYMBOLS = {
    "BTCUSD": "BTC/USD",
    "BTC/USD": "BTC/USD",
    "ETHUSD": "ETH/USD",
    "ETH/USD": "ETH/USD",
    "SOLUSD": "SOL/USD",
    "SOL/USD": "SOL/USD",
    "DOGEUSD": "DOGE/USD",
    "DOGE/USD": "DOGE/USD",
}


ETF_SYMBOLS = {
    "SPY", "QQQ", "IWM", "DIA", "VTI", "VOO", "IVV", "ARKK", "XLF", "XLK",
    "XLE", "XLV", "XLY", "XLP", "XLI", "XLU", "XLB", "XLRE", "GLD", "SLV",
}


def normalize_symbol(symbol: str) -> str:
    raw = symbol.strip().upper().replace("-", "/")
    return SUPPORTED_CRYPTO_SYMBOLS.get(raw, raw)


def is_supported_crypto(symbol: str) -> bool:
    return normalize_symbol(symbol) in set(SUPPORTED_CRYPTO_SYMBOLS.values())


def asset_type_for_symbol(symbol: str) -> AssetType:
    normalized = normalize_symbol(symbol)
    if is_supported_crypto(normalized):
        return "crypto"
    if normalized in ETF_SYMBOLS:
        return "etf"
    return "stock"


def should_skip_for_earnings(symbol: str, fmp_api_key: str | None = None, horizon_hours: int = 48) -> tuple[bool, str | None]:
    """
    Return True when a stock/ETF has an earnings event inside the next horizon.
    Crypto assets never have corporate earnings and are always allowed through.
    The function fails open when FMP is not configured or unavailable.
    """
    symbol = normalize_symbol(symbol)
    asset_type = asset_type_for_symbol(symbol)
    if asset_type == "crypto":
        return False, None

    api_key = fmp_api_key or config.FMP_API_KEY
    if not api_key:
        return False, None

    today = date.today()
    to_date = today + timedelta(hours=horizon_hours)
    try:
        resp = requests.get(
            f"https://financialmodelingprep.com/api/v3/historical/earning_calendar/{symbol}",
            params={
                "apikey": api_key,
                "from": today.isoformat(),
                "to": to_date.isoformat(),
            },
            timeout=10,
        )
        if not resp.ok:
            logger.warning("Earnings check failed for %s: HTTP %s", symbol, resp.status_code)
            return False, None
        events = resp.json()
        if isinstance(events, list) and events:
            event_date = str(events[0].get("date") or events[0].get("fiscalDateEnding") or "unknown")
            return True, f"earnings within {horizon_hours}h ({event_date})"
    except Exception as exc:
        logger.warning("Earnings check error for %s: %s", symbol, exc)
    return False, None


def score_crypto_technical_valuation(bars: list[dict]) -> dict[str, float]:
    """
    Crypto Scout valuation substitute. It avoids corporate accounting endpoints
    and uses price extension, volume z-score, and momentum from OHLCV bars.
    """
    if len(bars) < 20:
        return {"score_growth": 50.0, "score_value": 50.0, "score_sentiment": 50.0, "score_earnings_quality": 50.0}

    closes = [float(b["c"]) for b in bars]
    volumes = [float(b.get("v", 0)) for b in bars]
    current = closes[-1]
    ma20 = sum(closes[-20:]) / 20
    ma50 = sum(closes[-50:]) / 50 if len(closes) >= 50 else sum(closes) / len(closes)
    extension = (current - ma20) / max(ma20, 0.0001)
    momentum = (current - closes[-20]) / max(closes[-20], 0.0001)
    avg_volume = sum(volumes[-30:]) / min(len(volumes), 30)
    variance = sum((v - avg_volume) ** 2 for v in volumes[-30:]) / min(len(volumes), 30)
    vol_std = variance ** 0.5 if variance > 0 else 1.0
    volume_z = (volumes[-1] - avg_volume) / vol_std if volumes else 0.0

    score_growth = min(100.0, max(0.0, 50.0 + momentum * 250.0))
    score_value = min(100.0, max(0.0, 75.0 - abs(extension) * 250.0))
    score_sentiment = min(100.0, max(0.0, 50.0 + volume_z * 12.5))
    score_earnings_quality = min(100.0, max(0.0, 50.0 + ((ma20 - ma50) / max(ma50, 0.0001)) * 300.0))
    return {
        "score_growth": round(score_growth, 2),
        "score_value": round(score_value, 2),
        "score_sentiment": round(score_sentiment, 2),
        "score_earnings_quality": round(score_earnings_quality, 2),
    }


class BaseStrategy(ABC):
    name: str = "base"

    @abstractmethod
    def signal(self, bars: list[dict]) -> Signal:
        """Return a trading signal given recent bars."""
        ...

    def __str__(self):
        return self.name


# ------------------------------------------------------------------ #
#  Moving Average Crossover                                           #
# ------------------------------------------------------------------ #

class SMACrossover(BaseStrategy):
    """
    Buy when short-term SMA crosses above long-term SMA.
    Sell when it crosses below.
    """
    name = "sma_crossover"

    def __init__(self, short: int = 5, long: int = 20):
        self.short = short
        self.long = long

    def signal(self, bars: list[dict]) -> Signal:
        if len(bars) < self.long:
            return "hold"
        closes = [float(b["c"]) for b in bars]
        sma_short = sum(closes[-self.short:]) / self.short
        sma_long  = sum(closes[-self.long:])  / self.long
        prev_short = sum(closes[-(self.short+1):-1]) / self.short
        prev_long  = sum(closes[-(self.long+1):-1])  / self.long

        if prev_short <= prev_long and sma_short > sma_long:
            return "buy"
        if prev_short >= prev_long and sma_short < sma_long:
            return "sell"
        return "hold"


# ------------------------------------------------------------------ #
#  RSI Mean Reversion                                                 #
# ------------------------------------------------------------------ #

class RSIStrategy(BaseStrategy):
    """Buy when RSI < oversold, sell when RSI > overbought."""
    name = "rsi"

    def __init__(self, period: int = 14, oversold: float = 30, overbought: float = 70):
        self.period = period
        self.oversold = oversold
        self.overbought = overbought

    def _rsi(self, closes: list[float]) -> float:
        if len(closes) < self.period + 1:
            return 50.0
        deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
        gains  = [d for d in deltas[-self.period:] if d > 0]
        losses = [-d for d in deltas[-self.period:] if d < 0]
        avg_gain = sum(gains) / self.period if gains else 0
        avg_loss = sum(losses) / self.period if losses else 0
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100 - 100 / (1 + rs)

    def signal(self, bars: list[dict]) -> Signal:
        closes = [float(b["c"]) for b in bars]
        rsi = self._rsi(closes)
        if rsi < self.oversold:
            return "buy"
        if rsi > self.overbought:
            return "sell"
        return "hold"


# ------------------------------------------------------------------ #
#  VWAP Intraday                                                      #
# ------------------------------------------------------------------ #

class VWAPStrategy(BaseStrategy):
    """
    Buy when price dips below VWAP (mean-reversion entry).
    Sell when price rises above VWAP.
    """
    name = "vwap"

    def signal(self, bars: list[dict]) -> Signal:
        if not bars:
            return "hold"
        total_vol = sum(float(b["v"]) for b in bars)
        if total_vol == 0:
            return "hold"
        vwap = sum(float(b["v"]) * (float(b["h"]) + float(b["l"]) + float(b["c"])) / 3
                   for b in bars) / total_vol
        last_price = float(bars[-1]["c"])
        pct = (last_price - vwap) / vwap
        if pct < -0.005:    # 0.5% below VWAP
            return "buy"
        if pct > 0.005:
            return "sell"
        return "hold"


# ------------------------------------------------------------------ #
#  Registry                                                           #
# ------------------------------------------------------------------ #

STRATEGIES: dict[str, BaseStrategy] = {
    "sma_crossover": SMACrossover(),
    "rsi":           RSIStrategy(),
    "vwap":          VWAPStrategy(),
}


def get_strategy(name: str) -> BaseStrategy:
    if name not in STRATEGIES:
        raise ValueError(f"Unknown strategy '{name}'. Available: {list(STRATEGIES)}")
    return STRATEGIES[name]


# ── NexoSignal Phase 2: Market Regime Detection ────────────────────────────

def detect_market_regime(fred_api_key: str) -> dict:
    """
    Fetch DGS10 (10-yr yield), DGS2 (2-yr yield), and ICSA (weekly jobless claims)
    from the FRED REST API and classify the macro regime.

    Returns: {regime, dgs10, dgs2, spread, jobless_claims}
    Regimes: "risk_on" | "risk_off" | "stagflation" | "neutral"
    Zero-budget: FRED API is free with no usage limits.
    """
    if not fred_api_key:
        return {"regime": "neutral", "dgs10": None, "dgs2": None, "spread": None, "jobless_claims": None}

    def _fred_latest(series_id: str) -> Optional[float]:
        try:
            resp = requests.get(
                "https://api.stlouisfed.org/fred/series/observations",
                params={
                    "series_id": series_id,
                    "api_key": fred_api_key,
                    "file_type": "json",
                    "sort_order": "desc",
                    "limit": 5,
                },
                timeout=10,
            )
            if not resp.ok:
                return None
            for obs in resp.json().get("observations", []):
                val = obs.get("value", ".")
                if val != ".":
                    return float(val)
        except Exception as exc:
            logger.warning("FRED fetch failed for %s: %s", series_id, exc)
        return None

    dgs10 = _fred_latest("DGS10")
    dgs2 = _fred_latest("DGS2")
    jobless_claims = _fred_latest("ICSA")

    spread = round(dgs10 - dgs2, 3) if dgs10 is not None and dgs2 is not None else None

    regime = "neutral"
    if spread is not None and jobless_claims is not None:
        inverted = spread < 0
        elevated_claims = jobless_claims > 300_000
        if not inverted and not elevated_claims:
            regime = "risk_on"
        elif inverted and elevated_claims:
            regime = "risk_off"
        elif not inverted and elevated_claims:
            regime = "stagflation"
        else:
            regime = "risk_off"

    return {
        "regime": regime,
        "dgs10": dgs10,
        "dgs2": dgs2,
        "spread": spread,
        "jobless_claims": jobless_claims,
    }


# ── NexoSignal Scout: Smart Watchlist Engine ──────────────────────────────

def rebuild_watchlist(fmp_api_key: str, watchlist_size: int = 30) -> list[dict]:
    """
    NexoSignal Scout — score and rank stocks across 6 dimensions using FMP free tier.
    One screener call (1 of your 250 free daily calls) retrieves all needed data.

    Dimensions: growth, value, yield, sentiment, insider (placeholder), earnings_quality
    Returns: list of {symbol, composite_score, category, rank_position, score_*} dicts.
    """
    if not fmp_api_key:
        logger.warning("NexoSignal Scout: FMP_API_KEY not set, returning empty watchlist")
        return []

    try:
        resp = requests.get(
            "https://financialmodelingprep.com/api/v3/stock-screener",
            params={
                "apikey": fmp_api_key,
                "marketCapMoreThan": 1_000_000_000,
                "volumeMoreThan": 1_000_000,
                "isEtf": "false",
                "limit": 200,
            },
            timeout=15,
        )
        if not resp.ok:
            logger.error("NexoSignal Scout: screener failed %s", resp.status_code)
            return []
        screener_results = resp.json()
    except Exception as exc:
        logger.error("NexoSignal Scout: screener error: %s", exc)
        return []

    scored: list[dict] = []
    for item in screener_results[:200]:
        sym = item.get("symbol", "")
        if not sym or "." in sym:
            continue

        price = float(item.get("price") or 0)
        w52_high = float(item.get("yearHigh") or price or 1)
        w52_low = float(item.get("yearLow") or 0)
        year_range = max(w52_high - w52_low, 0.0001)
        price_position = (price - w52_low) / year_range
        score_growth = min(100.0, max(0.0, price_position * 100))

        pe = float(item.get("pe") or 0)
        if pe <= 0 or pe > 200:
            score_value = 10.0
        elif pe <= 10:
            score_value = 90.0
        elif pe <= 20:
            score_value = 70.0
        elif pe <= 30:
            score_value = 50.0
        else:
            score_value = max(0.0, 100.0 - pe)

        div = float(item.get("lastAnnualDividend") or 0)
        score_yield = min(100.0, (div / max(price, 0.0001)) * 100 * 20)

        volume = float(item.get("volume") or 0)
        avg_volume = float(item.get("avgVolume") or volume or 1)
        vol_ratio = volume / max(avg_volume, 1)
        score_sentiment = min(100.0, max(0.0, (vol_ratio - 0.5) / 1.5 * 100))

        score_insider = 50.0

        score_earnings_quality = min(100.0, max(0.0, score_growth * 0.5 + score_value * 0.5))

        composite = (
            score_growth * 0.25
            + score_value * 0.20
            + score_yield * 0.10
            + score_sentiment * 0.25
            + score_insider * 0.10
            + score_earnings_quality * 0.10
        )

        sector = (item.get("sector") or "").lower()
        if "tech" in sector or "informat" in sector:
            category = "tech"
        elif "financ" in sector or "bank" in sector:
            category = "finance"
        elif "health" in sector or "pharma" in sector or "biotech" in sector:
            category = "healthcare"
        elif "energy" in sector:
            category = "energy"
        elif "consumer" in sector:
            category = "consumer"
        else:
            category = "other"

        scored.append({
            "symbol": sym,
            "composite_score": round(composite, 2),
            "category": category,
            "score_growth": round(score_growth, 2),
            "score_value": round(score_value, 2),
            "score_yield": round(score_yield, 2),
            "score_sentiment": round(score_sentiment, 2),
            "score_insider": round(score_insider, 2),
            "score_earnings_quality": round(score_earnings_quality, 2),
        })

    scored.sort(key=lambda x: x["composite_score"], reverse=True)
    result = []
    for rank, entry in enumerate(scored[:watchlist_size], start=1):
        entry["rank_position"] = rank
        result.append(entry)

    logger.info("NexoSignal Scout rebuilt watchlist with %d symbols", len(result))
    return result


# ── Black-Litterman Portfolio Allocation ──────────────────────────────────

def calculate_bl_weights(picks: list, regime: str) -> dict[str, float]:
    """
    Black-Litterman-inspired portfolio weight allocation.
    Prior: equal weights. Views: probability × confluence score from AlphaCore.
    Regime tilt adjusts the momentum boost multiplier.

    picks: list of NexoSignalAlphaCoreCandidate (need .symbol, .probability, .confluence_score)
    Returns: {symbol: weight} that sums to 1.0
    """
    if not picks:
        return {}

    REGIME_BOOST = {
        "risk_on":     1.30,
        "risk_off":    0.70,
        "stagflation": 0.85,
        "neutral":     1.00,
    }
    boost = REGIME_BOOST.get(regime, 1.00)

    raw_weights: dict[str, float] = {}
    for pick in picks:
        raw = pick.probability * pick.confluence_score * boost
        raw_weights[pick.symbol] = max(raw, 0.0001)

    total = sum(raw_weights.values())
    weights = {sym: round(w / total, 4) for sym, w in raw_weights.items()}

    # Floating-point correction
    total2 = sum(weights.values())
    if weights and abs(total2 - 1.0) > 1e-6:
        first = next(iter(weights))
        weights[first] = round(weights[first] + (1.0 - total2), 4)

    return weights
