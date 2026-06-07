"""
All safety guardrails live here. Every order passes through check_order()
before being sent to the broker. If any rule is violated, a RejectedOrder
exception is raised with a human-readable reason.
"""

from dataclasses import dataclass, field
from datetime import date
from typing import Literal
from . import config


class RejectedOrder(Exception):
    pass


@dataclass
class DailyTracker:
    """Tracks per-day counters. Reset automatically when the date changes."""
    _date: date = field(default_factory=date.today)
    trade_count: int = 0
    realized_pnl: float = 0.0  # negative = loss

    def _maybe_reset(self):
        today = date.today()
        if today != self._date:
            self._date = today
            self.trade_count = 0
            self.realized_pnl = 0.0

    def record_trade(self, pnl: float = 0.0):
        self._maybe_reset()
        self.trade_count += 1
        self.realized_pnl += pnl

    @property
    def trades_today(self) -> int:
        self._maybe_reset()
        return self.trade_count

    @property
    def loss_today(self) -> float:
        self._maybe_reset()
        return min(self.realized_pnl, 0.0)  # always <= 0


# Singleton tracker shared across the session
_tracker = DailyTracker()


def get_tracker() -> DailyTracker:
    return _tracker


def check_order(
    symbol: str,
    side: Literal["buy", "sell"],
    qty: float,
    price: float,
    portfolio_value: float,
    available_cash: float,
):
    """
    Raise RejectedOrder if any restriction is violated.
    All checks are purely local — no network calls.
    """
    symbol = symbol.upper()
    order_value = qty * price

    # 1. Symbol whitelist
    if config.SYMBOL_WHITELIST and symbol not in config.SYMBOL_WHITELIST:
        raise RejectedOrder(
            f"{symbol} is not in the allowed symbol list: {sorted(config.SYMBOL_WHITELIST)}"
        )

    # 2. Symbol blacklist
    if symbol in config.SYMBOL_BLACKLIST:
        raise RejectedOrder(f"{symbol} is on the blocked symbol list.")

    # 3. Max order value
    if order_value > config.MAX_ORDER_VALUE_USD:
        raise RejectedOrder(
            f"Order value ${order_value:,.2f} exceeds limit ${config.MAX_ORDER_VALUE_USD:,.2f}."
        )

    # 4. Position size: buy orders must not exceed max % of portfolio
    if side == "buy":
        max_allowed = portfolio_value * config.MAX_POSITION_SIZE_PCT
        if order_value > max_allowed:
            raise RejectedOrder(
                f"Buy of ${order_value:,.2f} in {symbol} exceeds "
                f"{config.MAX_POSITION_SIZE_PCT*100:.0f}% position limit "
                f"(${max_allowed:,.2f} on ${portfolio_value:,.2f} portfolio)."
            )

    # 5. Cash reserve: buying must leave MIN_CASH_RESERVE_USD untouched
    if side == "buy":
        usable_cash = available_cash - config.MIN_CASH_RESERVE_USD
        if order_value > usable_cash:
            raise RejectedOrder(
                f"Insufficient usable cash. Available: ${available_cash:,.2f}, "
                f"Reserve: ${config.MIN_CASH_RESERVE_USD:,.2f}, "
                f"Order needs: ${order_value:,.2f}."
            )

    # 6. Daily trade count
    tracker = get_tracker()
    if tracker.trades_today >= config.MAX_TRADES_PER_DAY:
        raise RejectedOrder(
            f"Daily trade limit of {config.MAX_TRADES_PER_DAY} reached "
            f"({tracker.trades_today} trades today)."
        )

    # 7. Daily loss limit
    if portfolio_value > 0:
        loss_pct = abs(tracker.loss_today) / portfolio_value
        if loss_pct >= config.DAILY_LOSS_LIMIT_PCT:
            raise RejectedOrder(
                f"Daily loss limit hit: down {loss_pct*100:.2f}% today "
                f"(limit is {config.DAILY_LOSS_LIMIT_PCT*100:.0f}%). "
                f"No more trades until tomorrow."
            )

    # 8. HARD BLOCK — fund transfers are never allowed via this agent
    # (Alpaca's transfer endpoints are simply never called in broker.py,
    #  but this check exists as an explicit documented guardrail.)
    # Nothing to check here for a normal order — the broker layer enforces it.


def assert_no_transfer(action: str):
    """Call this from broker.py before any transfer-related API call."""
    raise RejectedOrder(
        f"Action '{action}' involves moving funds between your brokerage and bank. "
        "This agent is restricted from initiating fund transfers. "
        "Please manage deposits/withdrawals manually on the Alpaca dashboard."
    )
