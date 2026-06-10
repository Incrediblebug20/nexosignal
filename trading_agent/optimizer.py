"""
NexoSignal Strategy Optimizer.

Grid search over strategy parameter space with optional walk-forward validation.
Reuses NexoBacktester — all results are simulation-only.

Usage:
    from trading_agent.optimizer import StrategyOptimizer, OptimizeConfig
    cfg = OptimizeConfig(
        symbols=["AAPL"],
        strategy="sma_crossover",
        date_from="2022-01-01",
        date_to="2024-01-01",
    )
    results = StrategyOptimizer(cfg).run()
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, asdict
from typing import Any

from .backtester import BacktestConfig, BacktestResult, NexoBacktester

logger = logging.getLogger("trading_agent.optimizer")


# ── Parameter grids ───────────────────────────────────────────────────────────

_GRIDS: dict[str, list[dict]] = {
    "sma_crossover": [
        {"sma_short": s, "sma_long": l}
        for s in (3, 5, 8, 10)
        for l in (15, 20, 30, 50)
        if s < l
    ],
    "rsi": [
        {"rsi_period": p, "rsi_oversold": ov, "rsi_overbought": ob}
        for p in (7, 14, 21)
        for ov in (25.0, 30.0, 35.0)
        for ob in (65.0, 70.0, 75.0)
    ],
    "vwap": [
        {},  # VWAP has no tunable params yet; kept as single-run
    ],
}


# ── Config ────────────────────────────────────────────────────────────────────

@dataclass
class OptimizeConfig:
    symbols: list[str]
    strategy: str = "sma_crossover"
    date_from: str = ""
    date_to: str = ""
    initial_capital: float = 10_000.0
    commission_pct: float = 0.001
    slippage_pct: float = 0.0005
    max_position_pct: float = 0.20
    walk_forward: bool = True   # split data 70% IS / 30% OOS
    is_pct: float = 0.70
    max_workers: int = 4        # parallel backtests
    rank_by: str = "sharpe_ratio"  # "sharpe_ratio" | "total_return_pct" | "win_rate"
    top_n: int = 10             # return only top N results


@dataclass
class OptimizeRun:
    rank: int
    params: dict
    is_result: BacktestResult | None        # in-sample
    oos_result: BacktestResult | None       # out-of-sample (walk-forward)
    combined_score: float = 0.0

    def to_dict(self) -> dict:
        return {
            "rank":           self.rank,
            "params":         self.params,
            "combined_score": round(self.combined_score, 4),
            "is":  _result_summary(self.is_result),
            "oos": _result_summary(self.oos_result),
        }


@dataclass
class OptimizeResult:
    strategy: str
    symbols: list[str]
    date_from: str
    date_to: str
    walk_forward: bool
    rank_by: str
    runs: list[OptimizeRun] = field(default_factory=list)
    best_params: dict = field(default_factory=dict)
    duration_sec: float = 0.0
    total_combinations: int = 0
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "strategy":           self.strategy,
            "symbols":            self.symbols,
            "date_from":          self.date_from,
            "date_to":            self.date_to,
            "walk_forward":       self.walk_forward,
            "rank_by":            self.rank_by,
            "best_params":        self.best_params,
            "duration_sec":       self.duration_sec,
            "total_combinations": self.total_combinations,
            "runs":               [r.to_dict() for r in self.runs],
            "error":              self.error,
        }


# ── Optimizer ─────────────────────────────────────────────────────────────────

class StrategyOptimizer:
    """Grid-search optimizer with optional walk-forward validation."""

    def __init__(self, cfg: OptimizeConfig) -> None:
        self.cfg = cfg

    def run(self) -> OptimizeResult:
        t0 = time.time()
        cfg = self.cfg

        result = OptimizeResult(
            strategy=cfg.strategy,
            symbols=cfg.symbols,
            date_from=cfg.date_from,
            date_to=cfg.date_to,
            walk_forward=cfg.walk_forward,
            rank_by=cfg.rank_by,
        )

        grid = _GRIDS.get(cfg.strategy, [{}])
        result.total_combinations = len(grid)

        if not grid:
            result.error = f"No parameter grid for strategy '{cfg.strategy}'"
            return result

        # Split dates for walk-forward
        is_from, is_to, oos_from, oos_to = _split_dates(
            cfg.date_from, cfg.date_to, cfg.is_pct
        ) if cfg.walk_forward else (cfg.date_from, cfg.date_to, None, None)

        logger.info(
            "Optimizer: strategy=%s symbols=%s combos=%d WF=%s IS=[%s→%s] OOS=[%s→%s]",
            cfg.strategy, cfg.symbols, len(grid), cfg.walk_forward,
            is_from, is_to, oos_from, oos_to,
        )

        def _run_combo(params: dict) -> OptimizeRun:
            bt_cfg = BacktestConfig(
                symbols=cfg.symbols,
                strategy=cfg.strategy,
                date_from=is_from,
                date_to=is_to,
                initial_capital=cfg.initial_capital,
                commission_pct=cfg.commission_pct,
                slippage_pct=cfg.slippage_pct,
                max_position_pct=cfg.max_position_pct,
                **params,
            )
            is_res = NexoBacktester(bt_cfg).run()

            oos_res = None
            if cfg.walk_forward and oos_from and oos_to:
                oos_cfg = BacktestConfig(
                    symbols=cfg.symbols,
                    strategy=cfg.strategy,
                    date_from=oos_from,
                    date_to=oos_to,
                    initial_capital=cfg.initial_capital,
                    commission_pct=cfg.commission_pct,
                    slippage_pct=cfg.slippage_pct,
                    max_position_pct=cfg.max_position_pct,
                    **params,
                )
                oos_res = NexoBacktester(oos_cfg).run()

            # Combined score: IS score weighted 70%, OOS score 30% (if WF)
            is_score  = getattr(is_res, cfg.rank_by, 0.0) or 0.0
            oos_score = getattr(oos_res, cfg.rank_by, 0.0) if oos_res else is_score
            if cfg.walk_forward:
                combined = 0.7 * is_score + 0.3 * oos_score
            else:
                combined = is_score

            return OptimizeRun(
                rank=0,
                params=params,
                is_result=is_res,
                oos_result=oos_res,
                combined_score=combined,
            )

        # Run all combinations (parallel)
        opt_runs: list[OptimizeRun] = []
        with ThreadPoolExecutor(max_workers=min(cfg.max_workers, len(grid))) as pool:
            futures = {pool.submit(_run_combo, p): p for p in grid}
            for fut in as_completed(futures):
                try:
                    opt_runs.append(fut.result())
                except Exception as exc:
                    logger.warning("Optimizer combo failed: %s", exc)

        # Sort and rank
        opt_runs.sort(key=lambda r: r.combined_score, reverse=True)
        for i, r in enumerate(opt_runs[:cfg.top_n]):
            r.rank = i + 1

        result.runs = opt_runs[:cfg.top_n]
        result.best_params = opt_runs[0].params if opt_runs else {}
        result.duration_sec = round(time.time() - t0, 2)

        logger.info(
            "Optimizer done: %d combos in %.1fs best=%s score=%.4f",
            len(grid), result.duration_sec, result.best_params,
            opt_runs[0].combined_score if opt_runs else 0.0,
        )
        return result


# ── Helpers ───────────────────────────────────────────────────────────────────

def _split_dates(
    date_from: str,
    date_to: str,
    is_pct: float,
) -> tuple[str, str, str, str]:
    """Split a date range into IS and OOS segments."""
    from datetime import datetime, timedelta
    d0 = datetime.fromisoformat(date_from)
    d1 = datetime.fromisoformat(date_to)
    total_days = (d1 - d0).days
    split_days = int(total_days * is_pct)
    split_date = d0 + timedelta(days=split_days)
    is_to   = (split_date - timedelta(days=1)).strftime("%Y-%m-%d")
    oos_from = split_date.strftime("%Y-%m-%d")
    return date_from, is_to, oos_from, date_to


def _result_summary(r: BacktestResult | None) -> dict | None:
    if r is None:
        return None
    return {
        "total_return_pct":  r.total_return_pct,
        "max_drawdown_pct":  r.max_drawdown_pct,
        "sharpe_ratio":      r.sharpe_ratio,
        "win_rate":          r.win_rate,
        "num_trades":        r.num_trades,
        "final_equity":      r.final_equity,
        "profit_factor":     r.profit_factor,
    }
