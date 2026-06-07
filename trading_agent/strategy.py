"""
Pluggable trading strategies. Each strategy receives bars (OHLCV list)
and returns a signal: "buy", "sell", or "hold".

Add your own by subclassing BaseStrategy.
"""

from abc import ABC, abstractmethod
from typing import Literal


Signal = Literal["buy", "sell", "hold"]


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
