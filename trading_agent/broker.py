"""
Thin wrapper around the Alpaca REST API.
Only order execution, account info, and market data are exposed.
Fund transfer endpoints are deliberately absent.
"""

import aiohttp
import requests
from datetime import datetime, timezone
from typing import Optional
from . import config
from .restrictions import assert_no_transfer, check_order, get_tracker, RejectedOrder
from .strategy import is_supported_crypto, normalize_symbol


class BrokerError(Exception):
    pass


class AlpacaBroker:
    def __init__(self):
        if not config.ALPACA_API_KEY or not config.ALPACA_SECRET_KEY:
            raise BrokerError(
                "Missing API keys. Copy .env.example to .env and fill in your Alpaca credentials."
            )
        self._headers = {
            "APCA-API-KEY-ID": config.ALPACA_API_KEY,
            "APCA-API-SECRET-KEY": config.ALPACA_SECRET_KEY,
            "Content-Type": "application/json",
        }
        self._base = config.BASE_URL
        self._data_base = "https://data.alpaca.markets"

    # ------------------------------------------------------------------ #
    #  Internal helpers                                                    #
    # ------------------------------------------------------------------ #

    def _get(self, path: str, params: dict = None, base: str = None) -> dict:
        url = (base or self._base) + path
        r = requests.get(url, headers=self._headers, params=params, timeout=10)
        if not r.ok:
            raise BrokerError(f"GET {path} failed {r.status_code}: {r.text}")
        return r.json()

    def _post(self, path: str, body: dict) -> dict:
        url = self._base + path
        r = requests.post(url, headers=self._headers, json=body, timeout=10)
        if not r.ok:
            raise BrokerError(f"POST {path} failed {r.status_code}: {r.text}")
        return r.json()

    def _delete(self, path: str) -> None:
        url = self._base + path
        r = requests.delete(url, headers=self._headers, timeout=10)
        if not r.ok:
            raise BrokerError(f"DELETE {path} failed {r.status_code}: {r.text}")

    def _patch(self, path: str, body: dict) -> dict:
        url = self._base + path
        r = requests.patch(url, headers=self._headers, json=body, timeout=10)
        if not r.ok:
            raise BrokerError(f"PATCH {path} failed {r.status_code}: {r.text}")
        return r.json()

    # ------------------------------------------------------------------ #
    #  Account                                                             #
    # ------------------------------------------------------------------ #

    def get_account(self) -> dict:
        return self._get("/v2/account")

    def get_portfolio_value(self) -> float:
        acc = self.get_account()
        return float(acc["portfolio_value"])

    def get_cash(self) -> float:
        acc = self.get_account()
        return float(acc["cash"])

    def get_buying_power(self) -> float:
        acc = self.get_account()
        return float(acc["buying_power"])

    # ------------------------------------------------------------------ #
    #  Positions                                                           #
    # ------------------------------------------------------------------ #

    def get_positions(self) -> list[dict]:
        return self._get("/v2/positions")

    def get_position(self, symbol: str) -> Optional[dict]:
        try:
            return self._get(f"/v2/positions/{symbol.upper()}")
        except BrokerError:
            return None

    # ------------------------------------------------------------------ #
    #  Orders                                                              #
    # ------------------------------------------------------------------ #

    def get_orders(self, status: str = "open") -> list[dict]:
        return self._get("/v2/orders", params={"status": status, "limit": 100})

    def cancel_order(self, order_id: str) -> None:
        self._delete(f"/v2/orders/{order_id}")

    def cancel_all_orders(self) -> None:
        self._delete("/v2/orders")

    def replace_order(self, order_id: str, **fields) -> dict:
        return self._patch(f"/v2/orders/{order_id}", {k: str(v) for k, v in fields.items() if v is not None})

    def place_market_order(
        self,
        symbol: str,
        side: str,        # "buy" or "sell"
        qty: float,
        time_in_force: str = "day",
    ) -> dict:
        """Place a market order after passing all safety checks."""
        symbol = symbol.upper()

        # Fetch live price for restriction checks
        price = self.get_last_price(symbol)
        portfolio = self.get_portfolio_value()
        cash = self.get_cash()

        check_order(symbol, side, qty, price, portfolio, cash)

        body = {
            "symbol": symbol,
            "qty": str(qty),
            "side": side,
            "type": "market",
            "time_in_force": time_in_force,
        }
        result = self._post("/v2/orders", body)
        get_tracker().record_trade()
        return result

    def place_bracket_order(
        self,
        symbol: str,
        qty: int,
        entry_price: float,
        stop_loss: float,
        take_profit: float,
        time_in_force: str = "day",
    ) -> dict:
        """NexoSignal Executor bracket order using settled cash only."""
        symbol = normalize_symbol(symbol)
        if is_supported_crypto(symbol):
            raise RejectedOrder("Crypto bracket orders are not enabled in the safe initial rollout.")
        account = self.get_account()
        portfolio = float(account["portfolio_value"])
        cash = float(account["cash"])
        check_order(symbol, "buy", qty, entry_price, portfolio, cash)
        body = {
            "symbol": symbol,
            "qty": str(qty),
            "side": "buy",
            "type": "market",
            "time_in_force": time_in_force,
            "order_class": "bracket",
            "take_profit": {"limit_price": str(round(take_profit, 2))},
            "stop_loss": {"stop_price": str(round(stop_loss, 2))},
            "client_order_id": f"nexosignal-autonomous-{symbol}-{int(datetime.now(timezone.utc).timestamp())}",
        }
        result = self._post("/v2/orders", body)
        get_tracker().record_trade()
        return result

    def place_limit_order(
        self,
        symbol: str,
        side: str,
        qty: float,
        limit_price: float,
        time_in_force: str = "day",
    ) -> dict:
        """Place a limit order after passing all safety checks."""
        symbol = symbol.upper()
        portfolio = self.get_portfolio_value()
        cash = self.get_cash()

        check_order(symbol, side, qty, limit_price, portfolio, cash)

        body = {
            "symbol": symbol,
            "qty": str(qty),
            "side": side,
            "type": "limit",
            "limit_price": str(round(limit_price, 2)),
            "time_in_force": time_in_force,
        }
        result = self._post("/v2/orders", body)
        get_tracker().record_trade()
        return result

    # ------------------------------------------------------------------ #
    #  Market data                                                         #
    # ------------------------------------------------------------------ #

    def get_last_price(self, symbol: str) -> float:
        symbol = normalize_symbol(symbol)
        if is_supported_crypto(symbol):
            data = self._get(
                "/v1beta3/crypto/us/latest/trades",
                params={"symbols": symbol},
                base=self._data_base,
            )
            trade = (data.get("trades") or {}).get(symbol) or data.get("trade") or {}
            return float(trade["p"])
        data = self._get(
            f"/v2/stocks/{symbol.upper()}/trades/latest",
            base=self._data_base,
        )
        return float(data["trade"]["p"])

    def get_bars(self, symbol: str, timeframe: str = "1Min", limit: int = 60) -> list[dict]:
        """Return recent OHLCV bars."""
        symbol = normalize_symbol(symbol)
        if is_supported_crypto(symbol):
            data = self._get(
                "/v1beta3/crypto/us/bars",
                params={"symbols": symbol, "timeframe": timeframe, "limit": limit},
                base=self._data_base,
            )
            bars = data.get("bars", {})
            if isinstance(bars, dict):
                return bars.get(symbol, [])
            return bars if isinstance(bars, list) else []
        data = self._get(
            f"/v2/stocks/{symbol.upper()}/bars",
            params={"timeframe": timeframe, "limit": limit, "feed": "iex"},
            base=self._data_base,
        )
        return data.get("bars", [])

    def get_tradeable_stock_symbols(self, limit: int | None = None) -> list[str]:
        assets = self._get("/v2/assets", params={"status": "active", "asset_class": "us_equity"})
        symbols = [
            asset["symbol"].upper()
            for asset in assets
            if asset.get("tradable") and "." not in asset.get("symbol", "") and "/" not in asset.get("symbol", "")
        ]
        return symbols[:limit] if limit else symbols

    async def get_bulk_snapshots(self, symbols: list[str] | None = None) -> dict[str, dict]:
        """NexoSignal AlphaCore bulk snapshot request."""
        params = {"feed": "iex"}
        if symbols:
            params["symbols"] = ",".join(symbols)
        async with aiohttp.ClientSession(headers=self._headers) as session:
            async with session.get(f"{self._data_base}/v2/stocks/snapshots", params=params, timeout=30) as resp:
                if resp.status >= 400:
                    text = await resp.text()
                    raise BrokerError(f"GET /v2/stocks/snapshots failed {resp.status}: {text}")
                data = await resp.json()
        return data if isinstance(data, dict) else {}

    def get_quote(self, symbol: str) -> dict:
        data = self._get(
            f"/v2/stocks/{symbol.upper()}/quotes/latest",
            base=self._data_base,
        )
        return data.get("quote", {})

    def is_market_open(self) -> bool:
        clock = self._get("/v2/clock")
        return clock["is_open"]

    def get_clock(self) -> dict:
        return self._get("/v2/clock")

    # ------------------------------------------------------------------ #
    #  Fund transfer — BLOCKED                                             #
    # ------------------------------------------------------------------ #

    def transfer_funds(self, *args, **kwargs):
        assert_no_transfer("transfer_funds")

    def create_ach_relationship(self, *args, **kwargs):
        assert_no_transfer("create_ach_relationship")

    def initiate_withdrawal(self, *args, **kwargs):
        assert_no_transfer("initiate_withdrawal")

    def initiate_deposit(self, *args, **kwargs):
        assert_no_transfer("initiate_deposit")


class TelegramNotifier:
    """Free NexoSignal alert channel using the Telegram Bot API."""

    def __init__(self):
        self.token = config.TELEGRAM_TOKEN
        self.chat_id = config.TELEGRAM_CHAT_ID

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def send(self, message: str) -> None:
        if not self.enabled:
            return
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        requests.post(url, json={"chat_id": self.chat_id, "text": message}, timeout=10)

    def daily_picks_alert(self, picks: list) -> None:
        today = datetime.now().strftime("%Y-%m-%d")
        lines = [f"NexoSignal Alpha Picks - {today}"]
        for idx, pick in enumerate(picks, start=1):
            lines.append(f"{idx}. {pick.symbol} | Score: {pick.confluence_score:.0f} | Entry ~${pick.price:.2f}")
        lines.append("Circuit breaker: INACTIVE")
        self.send("\n".join(lines))

    def trade_opened(self, symbol: str, entry: float, qty: int, stop: float, target: float) -> None:
        self.send(
            f"Trade Opened - {symbol}\n"
            f"Entry: ${entry:.2f} | Qty: {qty}\n"
            f"Stop-loss: ${stop:.2f} | Target: ${target:.2f}\n"
            "Mode: Autonomous"
        )

    def trade_opened_detailed(
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
        self.send(
            "NexoSignal Executor\n"
            "Status: Autonomous Bracket Order Placed\n"
            f"Asset: {symbol} ({asset_type.upper()})\n"
            f"Capital: ${capital_allocated:,.2f}\n"
            f"Quantity: {qty}\n"
            f"AlphaCore: {confluence_score:.2f}/100\n"
            f"Entry: ${entry:.2f}\n"
            f"Take Profit: ${target:.2f}\n"
            f"Stop Loss: ${stop:.2f}"
        )

    def stop_adjusted(self, symbol: str, entry: float, current: float, atr_multiple: float) -> None:
        self.send(
            f"Stop Adjusted - {symbol}\n"
            f"Break-even stop set at ${entry:.2f}\n"
            f"Current price: ${current:.2f} (+{atr_multiple:.2f}x ATR)"
        )

    def trade_closed(self, symbol: str, entry: float, exit_price: float, pnl: float, pct: float, ratio: float) -> None:
        self.send(
            f"Trade Closed - {symbol}\n"
            f"Entry: ${entry:.2f} | Exit: ${exit_price:.2f}\n"
            f"PnL: ${pnl:.2f} ({pct:.2f}%)\n"
            f"RR achieved: {ratio:.2f}:1"
        )

    def eod_report(self, trades: int, wins: int, losses: int, pnl: float, breaker_active: bool, strikes: int) -> None:
        today = datetime.now().strftime("%Y-%m-%d")
        self.send(
            f"NexoSignal EOD Report - {today}\n"
            f"Trades executed: {trades}\n"
            f"Win / Loss: {wins}W / {losses}L\n"
            f"Net PnL: ${pnl:.2f}\n"
            f"Circuit breaker: {'ACTIVE' if breaker_active else 'INACTIVE'}\n"
            f"Strikes: {strikes}/2"
        )

    def circuit_breaker_tripped(self, reason: str) -> None:
        self.send(f"NexoSignal circuit breaker tripped.\nReason: {reason}")

    def send_premarket_briefing(
        self,
        picks: list,
        regime: dict,
        macro: dict | None,
        insiders: list[dict],
    ) -> None:
        """7 AM pre-market intelligence briefing sent to Telegram."""
        today = datetime.now().strftime("%Y-%m-%d %H:%M ET")
        lines = [f"NexoSignal Pre-Market Briefing — {today}"]

        reg = regime.get("regime", "neutral").upper()
        spread = regime.get("spread")
        claims = regime.get("jobless_claims")
        lines.append(
            f"Macro Regime: {reg}"
            + (f" | Yield spread: {spread:+.3f}%" if spread is not None else "")
            + (f" | Jobless claims: {claims:,.0f}" if claims is not None else "")
        )

        if picks:
            lines.append(f"\nTop AlphaCore picks ({len(picks)}):")
            for i, p in enumerate(picks[:5], start=1):
                lines.append(f"  {i}. {p.symbol} | Score {p.confluence_score:.0f} | ~${p.price:.2f}")

        if insiders:
            lines.append(f"\nRecent insider filings ({len(insiders)}):")
            for ins in insiders[:3]:
                tx = ins.get("transaction_type", "?")
                name = ins.get("insider_name", "Unknown")
                sym = ins.get("symbol", "")
                shares = ins.get("shares")
                lines.append(
                    f"  {sym} — {name}: {tx}"
                    + (f" {shares:,.0f} shares" if shares else "")
                )

        self.send("\n".join(lines))
