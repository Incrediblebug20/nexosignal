"""
NexoSignal Backtesting Engine.

Simulates historical strategy performance on OHLCV data.
All results are simulation-only — no live or paper orders are placed.
Data: yfinance (primary) → Alpaca historical API (fallback).

Usage:
    from trading_agent.backtester import NexoBacktester, BacktestConfig
    cfg = BacktestConfig(
        symbols=["AAPL", "MSFT"],
        strategy="sma_crossover",
        date_from="2023-01-01",
        date_to="2024-01-01",
        initial_capital=10000.0,
    )
    result = NexoBacktester(cfg).run()
"""

from __future__ import annotations

import logging
import math
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, date, timedelta
from typing import Any

from . import config

logger = logging.getLogger("trading_agent.backtester")

# ── Configuration ─────────────────────────────────────────────────────────────

@dataclass
class BacktestConfig:
    symbols: list[str]
    strategy: str = "sma_crossover"
    date_from: str = ""           # ISO date string "YYYY-MM-DD"
    date_to: str = ""
    initial_capital: float = 10_000.0
    commission_pct: float = 0.001  # 0.1%
    slippage_pct: float = 0.0005   # 0.05%
    max_position_pct: float = 0.20  # 20% of equity per position
    # strategy-specific params (overrides defaults)
    sma_short: int = 5
    sma_long: int = 20
    rsi_period: int = 14
    rsi_oversold: float = 30.0
    rsi_overbought: float = 70.0
    stop_atr_mult: float = 1.0
    take_atr_mult: float = 5.0

    def __post_init__(self) -> None:
        if not self.date_to:
            self.date_to = date.today().isoformat()
        if not self.date_from:
            self.date_from = (date.today() - timedelta(days=365)).isoformat()


# ── Result types ──────────────────────────────────────────────────────────────

@dataclass
class TradeRecord:
    symbol: str
    side: str                    # "buy" | "sell_short"
    entry_date: str
    entry_price: float
    qty: float
    exit_date: str = ""
    exit_price: float = 0.0
    pnl: float = 0.0
    pnl_pct: float = 0.0
    exit_reason: str = "open"   # "stop_loss" | "take_profit" | "signal" | "end_of_data"
    commission: float = 0.0
    stop_loss: float = 0.0
    take_profit: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class BacktestResult:
    run_id: str
    symbols: list[str]
    strategy: str
    params: dict
    date_from: str
    date_to: str
    initial_capital: float
    final_equity: float = 0.0
    total_return_pct: float = 0.0
    max_drawdown_pct: float = 0.0
    sharpe_ratio: float = 0.0
    sortino_ratio: float = 0.0
    win_rate: float = 0.0
    num_trades: int = 0
    num_wins: int = 0
    num_losses: int = 0
    avg_win_pct: float = 0.0
    avg_loss_pct: float = 0.0
    profit_factor: float = 0.0
    max_consecutive_losses: int = 0
    total_commission: float = 0.0
    equity_curve: list[dict] = field(default_factory=list)
    trade_log: list[dict] = field(default_factory=list)
    data_source: str = "unknown"
    duration_sec: float = 0.0
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


# ── Data fetching ─────────────────────────────────────────────────────────────

def _yf_symbol(symbol: str) -> str:
    """Convert NexoSignal symbol format to yfinance format."""
    return symbol.replace("/", "-").replace(" ", "")


def _fetch_bars_yfinance(symbol: str, date_from: str, date_to: str) -> list[dict]:
    """Fetch OHLCV bars via yfinance."""
    import yfinance as yf  # type: ignore
    yf_sym = _yf_symbol(symbol)
    df = yf.download(yf_sym, start=date_from, end=date_to, progress=False, auto_adjust=True)
    if df.empty:
        return []
    bars: list[dict] = []
    for idx, row in df.iterrows():
        bars.append({
            "t": idx.strftime("%Y-%m-%d"),
            "o": float(row["Open"].iloc[0] if hasattr(row["Open"], "iloc") else row["Open"]),
            "h": float(row["High"].iloc[0] if hasattr(row["High"], "iloc") else row["High"]),
            "l": float(row["Low"].iloc[0] if hasattr(row["Low"], "iloc") else row["Low"]),
            "c": float(row["Close"].iloc[0] if hasattr(row["Close"], "iloc") else row["Close"]),
            "v": float(row["Volume"].iloc[0] if hasattr(row["Volume"], "iloc") else row["Volume"]),
        })
    return bars


def _fetch_bars_alpaca(symbol: str, date_from: str, date_to: str) -> list[dict]:
    """Fetch OHLCV bars via Alpaca historical API."""
    from .broker import AlpacaBroker, BrokerError
    try:
        broker = AlpacaBroker()
        # Estimate bar count from date range
        d0 = datetime.fromisoformat(date_from)
        d1 = datetime.fromisoformat(date_to)
        days = max(1, (d1 - d0).days)
        # Alpaca's get_bars uses limit, not date range — fetch generously
        raw = broker.get_bars(symbol, timeframe="1Day", limit=min(days + 20, 1000))
        if not raw:
            return []
        # Filter to requested date range
        bars = []
        for b in raw:
            t = str(b.get("t", ""))[:10]
            if date_from <= t <= date_to:
                bars.append(b)
        return bars
    except Exception as exc:
        logger.warning("Alpaca bar fetch failed for %s: %s", symbol, exc)
        return []


def fetch_bars(symbol: str, date_from: str, date_to: str) -> tuple[list[dict], str]:
    """
    Fetch OHLCV bars — yfinance primary, Alpaca fallback.
    Returns (bars, source_label).
    """
    try:
        bars = _fetch_bars_yfinance(symbol, date_from, date_to)
        if bars:
            return bars, "yfinance"
    except ImportError:
        logger.info("yfinance not installed, falling back to Alpaca")
    except Exception as exc:
        logger.warning("yfinance fetch failed for %s: %s", symbol, exc)

    bars = _fetch_bars_alpaca(symbol, date_from, date_to)
    return bars, "alpaca"


# ── ATR helper ────────────────────────────────────────────────────────────────

def _atr(bars: list[dict], period: int = 14) -> float:
    if len(bars) < 2:
        return 0.0
    trs: list[float] = []
    for i in range(1, min(period + 1, len(bars))):
        h = float(bars[-i].get("h", bars[-i]["c"]))
        l = float(bars[-i].get("l", bars[-i]["c"]))
        prev_c = float(bars[-(i + 1)]["c"])
        trs.append(max(h - l, abs(h - prev_c), abs(l - prev_c)))
    return sum(trs) / len(trs) if trs else 0.0


# ── Signal generators (vectorised over bar window) ────────────────────────────

def _signal_sma(bars: list[dict], short: int, long: int) -> str:
    if len(bars) < long + 1:
        return "hold"
    closes = [float(b["c"]) for b in bars]
    sma_s  = sum(closes[-short:]) / short
    sma_l  = sum(closes[-long:])  / long
    prev_s = sum(closes[-(short + 1):-1]) / short
    prev_l = sum(closes[-(long + 1):-1])  / long
    if prev_s <= prev_l and sma_s > sma_l:
        return "buy"
    if prev_s >= prev_l and sma_s < sma_l:
        return "sell"
    return "hold"


def _signal_rsi(bars: list[dict], period: int, oversold: float, overbought: float) -> str:
    if len(bars) < period + 1:
        return "hold"
    closes = [float(b["c"]) for b in bars]
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains  = [max(d, 0.0) for d in deltas[-period:]]
    losses = [max(-d, 0.0) for d in deltas[-period:]]
    ag = sum(gains) / period
    al = sum(losses) / period
    if al == 0:
        rsi = 100.0
    else:
        rsi = 100.0 - 100.0 / (1.0 + ag / al)
    if rsi < oversold:
        return "buy"
    if rsi > overbought:
        return "sell"
    return "hold"


def _signal_vwap(bars: list[dict], threshold: float = 0.005) -> str:
    if not bars:
        return "hold"
    total_vol = sum(float(b["v"]) for b in bars)
    if total_vol == 0:
        return "hold"
    vwap = sum(float(b["v"]) * (float(b["h"]) + float(b["l"]) + float(b["c"])) / 3
               for b in bars) / total_vol
    price = float(bars[-1]["c"])
    pct = (price - vwap) / vwap if vwap > 0 else 0
    if pct < -threshold:
        return "buy"
    if pct > threshold:
        return "sell"
    return "hold"


# ── Per-symbol simulation ─────────────────────────────────────────────────────

@dataclass
class _Position:
    symbol: str
    side: str
    entry_date: str
    entry_price: float
    qty: float
    stop_loss: float
    take_profit: float
    commission: float


def _simulate_symbol(
    symbol: str,
    bars: list[dict],
    cfg: BacktestConfig,
    starting_cash: float,
) -> tuple[list[TradeRecord], list[tuple[str, float]]]:
    """
    Simulate one symbol through the full bar history.
    Returns (trade_records, [(date, equity_contribution)] ).
    Equity contribution = cash + open mark-to-market value.
    """
    cash = starting_cash
    position: _Position | None = None
    trades: list[TradeRecord] = []
    equity_series: list[tuple[str, float]] = []

    def _get_signal(window: list[dict]) -> str:
        s = cfg.strategy
        if s == "sma_crossover":
            return _signal_sma(window, cfg.sma_short, cfg.sma_long)
        elif s == "rsi":
            return _signal_rsi(window, cfg.rsi_period, cfg.rsi_oversold, cfg.rsi_overbought)
        elif s == "vwap":
            return _signal_vwap(window)
        return "hold"

    for i, bar in enumerate(bars):
        bar_date = bar["t"][:10]
        open_p   = float(bar["o"])
        high_p   = float(bar["h"])
        low_p    = float(bar["l"])
        close_p  = float(bar["c"])

        # Check stop-loss / take-profit for open position first
        if position is not None:
            hit_sl = hit_tp = False
            if position.side == "buy":
                hit_sl = low_p  <= position.stop_loss
                hit_tp = high_p >= position.take_profit
            else:
                hit_sl = high_p >= position.stop_loss
                hit_tp = low_p  <= position.take_profit

            if hit_tp:
                exit_price = position.take_profit
                exit_reason = "take_profit"
            elif hit_sl:
                exit_price = position.stop_loss
                exit_reason = "stop_loss"
            else:
                exit_price = None
                exit_reason = None

            if exit_price is not None:
                exit_slip = exit_price * cfg.slippage_pct
                if position.side == "buy":
                    proceeds = position.qty * (exit_price - exit_slip)
                    pnl = proceeds - position.qty * position.entry_price - position.commission
                else:
                    proceeds = position.qty * position.entry_price
                    pnl = proceeds - position.qty * (exit_price + exit_slip) - position.commission
                exit_comm = position.qty * exit_price * cfg.commission_pct
                pnl -= exit_comm
                pnl_pct = pnl / (position.qty * position.entry_price) * 100
                cash += (position.qty * position.entry_price) + pnl
                trades.append(TradeRecord(
                    symbol=symbol,
                    side=position.side,
                    entry_date=position.entry_date,
                    entry_price=round(position.entry_price, 4),
                    qty=round(position.qty, 6),
                    exit_date=bar_date,
                    exit_price=round(exit_price, 4),
                    pnl=round(pnl, 4),
                    pnl_pct=round(pnl_pct, 4),
                    exit_reason=exit_reason,
                    commission=round(position.commission + exit_comm, 4),
                    stop_loss=round(position.stop_loss, 4),
                    take_profit=round(position.take_profit, 4),
                ))
                position = None

        # Compute signal using window up to and including current bar
        window = bars[max(0, i - 59): i + 1]  # 60-bar rolling window
        signal = _get_signal(window)

        # Entry logic — only enter if flat
        if position is None and signal in ("buy", "sell") and open_p > 0:
            # Entry on next bar open (use next bar's open if available, else close)
            entry_price = open_p
            # Apply slippage
            if signal == "buy":
                entry_price *= (1 + cfg.slippage_pct)
            else:
                entry_price *= (1 - cfg.slippage_pct)

            # ATR-based stop/take
            atr_val = _atr(bars[:i + 1])
            if atr_val <= 0:
                atr_val = entry_price * 0.02  # fallback: 2%

            if signal == "buy":
                sl = entry_price - cfg.stop_atr_mult * atr_val
                tp = entry_price + cfg.take_atr_mult * atr_val
            else:
                sl = entry_price + cfg.stop_atr_mult * atr_val
                tp = entry_price - cfg.take_atr_mult * atr_val

            # Position sizing: max_position_pct of current cash
            max_spend = cash * cfg.max_position_pct
            qty = math.floor(max_spend / entry_price * 1000) / 1000  # round to 3dp
            if qty <= 0 or cash < entry_price:
                pass  # can't afford
            else:
                comm = qty * entry_price * cfg.commission_pct
                spend = qty * entry_price + comm
                if spend <= cash:
                    cash -= spend
                    position = _Position(
                        symbol=symbol,
                        side=signal,
                        entry_date=bar_date,
                        entry_price=entry_price,
                        qty=qty,
                        stop_loss=sl,
                        take_profit=tp,
                        commission=comm,
                    )

        # Mark-to-market equity for this bar
        mtm = position.qty * close_p if position else 0.0
        equity_series.append((bar_date, cash + mtm))

    # Close any open position at end of data
    if position is not None and bars:
        last_bar = bars[-1]
        exit_price = float(last_bar["c"])
        exit_comm = position.qty * exit_price * cfg.commission_pct
        if position.side == "buy":
            pnl = position.qty * (exit_price - position.entry_price) - position.commission - exit_comm
        else:
            pnl = position.qty * (position.entry_price - exit_price) - position.commission - exit_comm
        pnl_pct = pnl / (position.qty * position.entry_price) * 100
        cash += (position.qty * position.entry_price) + pnl
        trades.append(TradeRecord(
            symbol=symbol,
            side=position.side,
            entry_date=position.entry_date,
            entry_price=round(position.entry_price, 4),
            qty=round(position.qty, 6),
            exit_date=last_bar["t"][:10],
            exit_price=round(exit_price, 4),
            pnl=round(pnl, 4),
            pnl_pct=round(pnl_pct, 4),
            exit_reason="end_of_data",
            commission=round(position.commission + exit_comm, 4),
            stop_loss=round(position.stop_loss, 4),
            take_profit=round(position.take_profit, 4),
        ))

    return trades, equity_series


# ── Metrics ───────────────────────────────────────────────────────────────────

def _compute_metrics(
    equity_curve: list[float],
    trades: list[TradeRecord],
    initial_capital: float,
) -> dict:
    """Compute backtest performance metrics from equity curve and trade log."""
    if not equity_curve:
        return {}

    final_equity = equity_curve[-1]
    total_return_pct = (final_equity - initial_capital) / initial_capital * 100

    # Max drawdown
    peak = equity_curve[0]
    max_dd = 0.0
    for eq in equity_curve:
        if eq > peak:
            peak = eq
        dd = (peak - eq) / peak if peak > 0 else 0.0
        max_dd = max(max_dd, dd)

    # Sharpe ratio (annualised, assuming daily returns, risk-free ≈ 0)
    if len(equity_curve) > 1:
        daily_returns = [
            (equity_curve[i] - equity_curve[i - 1]) / equity_curve[i - 1]
            for i in range(1, len(equity_curve))
            if equity_curve[i - 1] > 0
        ]
        if daily_returns:
            mean_r = sum(daily_returns) / len(daily_returns)
            variance = sum((r - mean_r) ** 2 for r in daily_returns) / len(daily_returns)
            std_r = variance ** 0.5
            sharpe = (mean_r / std_r * (252 ** 0.5)) if std_r > 0 else 0.0

            # Sortino (downside deviation)
            neg_returns = [r for r in daily_returns if r < 0]
            if neg_returns:
                down_var = sum(r ** 2 for r in neg_returns) / len(neg_returns)
                down_std = down_var ** 0.5
                sortino = (mean_r / down_std * (252 ** 0.5)) if down_std > 0 else 0.0
            else:
                sortino = float("inf")
        else:
            sharpe = sortino = 0.0
    else:
        sharpe = sortino = 0.0

    # Trade stats
    closed = [t for t in trades if t.exit_reason != "open"]
    wins   = [t for t in closed if t.pnl > 0]
    losses = [t for t in closed if t.pnl <= 0]
    win_rate = len(wins) / len(closed) * 100 if closed else 0.0
    avg_win  = sum(t.pnl_pct for t in wins)   / len(wins)   if wins   else 0.0
    avg_loss = sum(t.pnl_pct for t in losses)  / len(losses) if losses else 0.0
    gross_profit = sum(t.pnl for t in wins)
    gross_loss   = abs(sum(t.pnl for t in losses))
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float("inf")
    total_commission = sum(t.commission for t in trades)

    # Max consecutive losses
    max_consec = consec = 0
    for t in closed:
        if t.pnl <= 0:
            consec += 1
            max_consec = max(max_consec, consec)
        else:
            consec = 0

    return {
        "final_equity":          round(final_equity, 2),
        "total_return_pct":      round(total_return_pct, 4),
        "max_drawdown_pct":      round(max_dd * 100, 4),
        "sharpe_ratio":          round(sharpe, 4),
        "sortino_ratio":         round(sortino, 4) if sortino != float("inf") else 999.0,
        "win_rate":              round(win_rate, 4),
        "num_trades":            len(closed),
        "num_wins":              len(wins),
        "num_losses":            len(losses),
        "avg_win_pct":           round(avg_win, 4),
        "avg_loss_pct":          round(avg_loss, 4),
        "profit_factor":         round(profit_factor, 4) if profit_factor != float("inf") else 999.0,
        "max_consecutive_losses": max_consec,
        "total_commission":      round(total_commission, 2),
    }


# ── Main engine ───────────────────────────────────────────────────────────────

class NexoBacktester:
    """
    Run historical strategy simulations.
    Simulation-only — never places live or paper orders.
    """

    def __init__(self, cfg: BacktestConfig) -> None:
        self.cfg = cfg

    def run(self) -> BacktestResult:
        t0 = time.time()
        run_id = str(uuid.uuid4())[:8]
        cfg = self.cfg

        result = BacktestResult(
            run_id=run_id,
            symbols=cfg.symbols,
            strategy=cfg.strategy,
            params={
                "sma_short": cfg.sma_short,
                "sma_long": cfg.sma_long,
                "rsi_period": cfg.rsi_period,
                "rsi_oversold": cfg.rsi_oversold,
                "rsi_overbought": cfg.rsi_overbought,
                "stop_atr_mult": cfg.stop_atr_mult,
                "take_atr_mult": cfg.take_atr_mult,
                "max_position_pct": cfg.max_position_pct,
                "commission_pct": cfg.commission_pct,
                "slippage_pct": cfg.slippage_pct,
            },
            date_from=cfg.date_from,
            date_to=cfg.date_to,
            initial_capital=cfg.initial_capital,
        )

        if not cfg.symbols:
            result.error = "No symbols provided"
            return result

        # Allocate equal starting capital per symbol
        per_symbol_capital = cfg.initial_capital / len(cfg.symbols)

        all_trades: list[TradeRecord] = []
        # equity_by_date maps date → combined equity across all symbols
        equity_by_date: dict[str, float] = {}
        data_sources: list[str] = []

        for symbol in cfg.symbols:
            try:
                bars, source = fetch_bars(symbol, cfg.date_from, cfg.date_to)
            except Exception as exc:
                logger.warning("Could not fetch bars for %s: %s", symbol, exc)
                bars, source = [], "error"

            data_sources.append(source)

            if not bars:
                logger.warning("No bar data for %s in [%s, %s]", symbol, cfg.date_from, cfg.date_to)
                # Add flat equity contribution
                for d in _date_range(cfg.date_from, cfg.date_to):
                    equity_by_date[d] = equity_by_date.get(d, 0.0) + per_symbol_capital
                continue

            try:
                trades, eq_series = _simulate_symbol(symbol, bars, cfg, per_symbol_capital)
            except Exception as exc:
                logger.exception("Simulation failed for %s: %s", symbol, exc)
                result.error = f"Simulation error for {symbol}: {exc}"
                return result

            all_trades.extend(trades)
            for dt, eq in eq_series:
                equity_by_date[dt] = equity_by_date.get(dt, 0.0) + eq

        # Build combined equity curve, sorted by date
        sorted_dates = sorted(equity_by_date.keys())
        combined_eq  = [equity_by_date[d] for d in sorted_dates]

        # Downsample equity curve to at most 500 points for payload efficiency
        curve = _downsample(
            [{"time": d, "value": round(v, 2)} for d, v in zip(sorted_dates, combined_eq)],
            max_points=500,
        )

        metrics = _compute_metrics(combined_eq, all_trades, cfg.initial_capital)

        result.equity_curve = curve
        result.trade_log    = [t.to_dict() for t in all_trades[:500]]
        result.data_source  = ", ".join(set(data_sources))
        result.duration_sec = round(time.time() - t0, 2)

        for k, v in metrics.items():
            if hasattr(result, k):
                setattr(result, k, v)

        logger.info(
            "Backtest %s: %s strategy=%s return=%.2f%% trades=%d sharpe=%.2f in %.1fs",
            run_id, cfg.symbols, cfg.strategy,
            result.total_return_pct, result.num_trades,
            result.sharpe_ratio, result.duration_sec,
        )

        # Persist to storage (non-fatal)
        _persist_result(result)

        return result


# ── Helpers ───────────────────────────────────────────────────────────────────

def _date_range(date_from: str, date_to: str) -> list[str]:
    """Generate list of ISO date strings between date_from and date_to."""
    d0 = datetime.fromisoformat(date_from).date()
    d1 = datetime.fromisoformat(date_to).date()
    result = []
    current = d0
    while current <= d1:
        result.append(current.isoformat())
        current += timedelta(days=1)
    return result


def _downsample(series: list[dict], max_points: int) -> list[dict]:
    """Keep first, last, and evenly-spaced points up to max_points."""
    if len(series) <= max_points:
        return series
    step = len(series) / max_points
    indices = {0, len(series) - 1}
    indices.update(int(i * step) for i in range(1, max_points - 1))
    return [series[i] for i in sorted(indices)]


def _persist_result(result: BacktestResult) -> None:
    """Store backtest summary to Supabase (non-fatal — silently no-ops without DB)."""
    try:
        from .storage import record_backtest_run
        record_backtest_run(
            run_id=result.run_id,
            symbols=",".join(result.symbols),
            strategy=result.strategy,
            params=result.params,
            date_from=result.date_from,
            date_to=result.date_to,
            initial_capital=result.initial_capital,
            final_equity=result.final_equity,
            total_return_pct=result.total_return_pct,
            max_drawdown_pct=result.max_drawdown_pct,
            sharpe_ratio=result.sharpe_ratio,
            win_rate=result.win_rate,
            num_trades=result.num_trades,
            data_source=result.data_source,
        )
    except Exception as exc:
        logger.debug("Could not persist backtest result: %s", exc)
