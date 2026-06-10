"""
All safety guardrails live here. Every order passes through check_order()
before being sent to the broker. If any rule is violated, a RejectedOrder
exception is raised with a human-readable reason.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
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
    guard_strikes: int = 0
    circuit_breaker_active: bool = False
    circuit_breaker_reason: str | None = None

    def _maybe_reset(self):
        today = date.today()
        if today != self._date:
            self._date = today
            self.trade_count = 0
            self.realized_pnl = 0.0
            self.guard_strikes = 0
            self.circuit_breaker_active = False
            self.circuit_breaker_reason = None

    def record_trade(self, pnl: float = 0.0):
        self._maybe_reset()
        self.trade_count += 1
        self.realized_pnl += pnl

    def record_guard_strike(self, reason: str) -> None:
        self._maybe_reset()
        self.guard_strikes += 1
        if self.trade_count >= 2 and self.guard_strikes >= config.MAX_GUARD_STRIKES:
            self.circuit_breaker_active = True
            self.circuit_breaker_reason = reason

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


@dataclass(frozen=True)
class GuardDecision:
    allowed: bool
    reason: str | None = None
    circuit_breaker_active: bool = False
    strikes: int = 0


def require_live_execution_enabled(
    *,
    market_open: bool,
    buying_power_verified: bool,
    supabase_healthy: bool,
    risk_checks_passed: bool,
    circuit_breaker_active: bool,
) -> GuardDecision:
    """Hard gate for any live autonomous order path."""
    failures: list[str] = []
    if not config.LIVE_TRADING:
        failures.append("LIVE_TRADING is not true")
    if not config.AUTONOMOUS_TRADING:
        failures.append("AUTONOMOUS_TRADING is not true")
    if config.REQUIRE_MANUAL_APPROVAL:
        failures.append("manual approval is required")
    if config.ALPACA_PAPER or config.TRADING_MODE == "paper":
        failures.append("Alpaca paper mode is enabled")
    if not market_open:
        failures.append("market is closed")
    if not buying_power_verified:
        failures.append("buying power cannot be verified")
    if not supabase_healthy:
        failures.append("Supabase persistence is not healthy")
    if not risk_checks_passed:
        failures.append("risk checks did not pass")
    if circuit_breaker_active:
        failures.append("circuit breaker active")
    return GuardDecision(not failures, "; ".join(failures) if failures else None, circuit_breaker_active, get_tracker().guard_strikes)


def check_autonomous_trade_requirements(
    *,
    confluence_score: float,
    risk_reward_ratio: float,
    atr: float,
    liquidity_valid: bool,
    stale_data: bool,
    buying_power_verified: bool,
    market_open: bool,
    supabase_healthy: bool,
    live_autonomous_mode: bool,
    circuit_breaker_active: bool | None = None,
) -> GuardDecision:
    tracker = get_tracker()
    breaker = tracker.circuit_breaker_active if circuit_breaker_active is None else circuit_breaker_active
    failures: list[str] = []
    if breaker:
        failures.append("circuit breaker active")
    if confluence_score < config.SIGNAL_MIN_CONFIDENCE:
        failures.append(f"confluence below {config.SIGNAL_MIN_CONFIDENCE:.0f}")
    if risk_reward_ratio + 1e-6 < config.MIN_RISK_REWARD_RATIO:
        failures.append(f"reward/risk below {config.MIN_RISK_REWARD_RATIO:.1f}:1")
    if atr <= 0:
        failures.append("missing ATR")
    if not liquidity_valid:
        failures.append("liquidity invalid")
    if stale_data:
        failures.append("stale data detected")
    if not buying_power_verified:
        failures.append("buying power cannot be verified")
    if not market_open:
        failures.append("market closed")
    if live_autonomous_mode and not supabase_healthy:
        failures.append("Supabase persistence failed during live autonomous mode")

    if failures:
        reason = "; ".join(failures)
        tracker.record_guard_strike(reason)
        return GuardDecision(False, reason, tracker.circuit_breaker_active, tracker.guard_strikes)
    return GuardDecision(True, None, tracker.circuit_breaker_active, tracker.guard_strikes)


def is_data_stale(timestamp: datetime | None, max_age_seconds: int | None = None) -> bool:
    if timestamp is None:
        return True
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - timestamp).total_seconds()
    return age > (max_age_seconds or config.MAX_DATA_STALENESS_SECONDS)


def break_even_stop_update(entry: float, current_price: float, atr: float, side: Literal["buy", "sell"] = "buy") -> tuple[bool, float]:
    if atr <= 0:
        return False, entry
    if side == "buy" and current_price >= entry + (2 * atr):
        return True, entry
    if side == "sell" and current_price <= entry - (2 * atr):
        return True, entry
    return False, entry


def trigger_circuit_breaker(reason: str) -> GuardDecision:
    tracker = get_tracker()
    tracker.circuit_breaker_active = True
    tracker.circuit_breaker_reason = reason
    return GuardDecision(False, reason, True, tracker.guard_strikes)


def assert_no_transfer(action: str):
    """Call this from broker.py before any transfer-related API call."""
    raise RejectedOrder(
        f"Action '{action}' involves moving funds between your brokerage and bank. "
        "This agent is restricted from initiating fund transfers. "
        "Please manage deposits/withdrawals manually on the Alpaca dashboard."
    )


# ── Phase 2: NexoSignal Guard Extensions ──────────────────────────────────

def calculate_position_risk(price: float, qty: float, atr: float) -> tuple[float, float]:
    """
    Compute 1-day Value-at-Risk for a single position.
    Uses 1.65 σ (95 % confidence), treating ATR as daily σ proxy.
    Returns (var_1d_dollars, var_1d_pct_of_position).
    """
    position_value = max(price * qty, 0.0001)
    var_1d = 1.65 * atr * qty
    var_1d_pct = var_1d / position_value
    return round(var_1d, 2), round(var_1d_pct, 4)


def check_portfolio_var(positions: list[dict], portfolio_value: float) -> None:
    """
    Raise RejectedOrder if the sum of per-position 1-day VaR estimates exceeds
    config.MAX_PORTFOLIO_VAR as a fraction of total portfolio value.

    Each entry in `positions` must have a 'var_1d' key (float, dollars).
    Positions without a var_1d key are skipped (conservative: assume zero).
    """
    if portfolio_value <= 0:
        return
    total_var = sum(float(p.get("var_1d") or 0.0) for p in positions)
    portfolio_var_pct = total_var / portfolio_value
    if portfolio_var_pct > config.MAX_PORTFOLIO_VAR:
        raise RejectedOrder(
            f"NexoSignal Guard: portfolio VaR {portfolio_var_pct*100:.2f}% exceeds the "
            f"{config.MAX_PORTFOLIO_VAR*100:.2f}% daily limit. No new positions until risk reduces."
        )


# Static sector-bucket map for correlation detection — no API required
_SECTOR_BUCKETS: dict[str, set[str]] = {
    "tech":        {"AAPL", "MSFT", "GOOGL", "META", "NVDA", "AMD", "INTC", "AVGO", "QCOM", "CRM", "ORCL"},
    "finance":     {"JPM", "BAC", "GS", "MS", "WFC", "C", "BLK", "AXP", "V", "MA"},
    "energy":      {"XOM", "CVX", "COP", "SLB", "OXY", "VLO", "MPC", "PSX"},
    "consumer":    {"AMZN", "TSLA", "NKE", "SBUX", "MCD", "TGT", "HD", "LOW", "COST"},
    "healthcare":  {"UNH", "JNJ", "LLY", "PFE", "ABBV", "MRK", "TMO", "ABT", "DHR"},
    "broad_etf":   {"SPY", "QQQ", "IWM", "DIA", "VTI", "VOO"},
}


def check_correlation(symbol: str, active_symbols: list[str]) -> None:
    """
    Block a new entry when the portfolio already holds 2+ symbols in the same
    sector bucket — using a static lookup, so no API calls required.
    Raises RejectedOrder if the symbol would exceed the per-bucket limit.
    """
    symbol = symbol.upper()
    active_set = {s.upper() for s in active_symbols}

    for bucket_name, bucket in _SECTOR_BUCKETS.items():
        if symbol not in bucket:
            continue
        overlap = bucket & active_set
        if len(overlap) >= 2:
            raise RejectedOrder(
                f"NexoSignal Guard: correlation blocked — {symbol} is in the '{bucket_name}' "
                f"sector bucket. Portfolio already holds {sorted(overlap)}. "
                f"Maximum 2 symbols per sector bucket."
            )
