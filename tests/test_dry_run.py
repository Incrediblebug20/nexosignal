"""Integration tests: dry-run mode and circuit breaker logic."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, call, patch


def _make_mock_broker(signal="buy"):
    """Return a broker mock that produces a configurable signal."""
    broker = MagicMock()
    broker._base = "https://paper-api.alpaca.markets"
    broker.is_market_open.return_value = True
    broker.get_account.return_value = {
        "portfolio_value": "100000",
        "cash": "50000",
        "buying_power": "50000",
    }
    broker.get_clock.return_value = {"is_open": True}
    broker.get_positions.return_value = []
    broker.get_position.return_value = None
    # Return 60 minimal bar dicts so analyze_signal has enough data
    bar = {"o": "150", "h": "151", "l": "149", "c": "150.5", "v": "1000000"}
    broker.get_bars.return_value = [bar] * 60
    return broker


def _make_agent(mock_broker, dry_run=True):
    with (
        patch("trading_agent.agent.AlpacaBroker", return_value=mock_broker),
        patch("trading_agent.agent.TelegramNotifier", return_value=MagicMock()),
        patch("trading_agent.storage._storage_enabled", return_value=False),
        patch("trading_agent.storage.record_signal_event"),
        patch("trading_agent.storage.record_trade_event"),
        patch("trading_agent.storage.record_agent_event"),
    ):
        from trading_agent.agent import NexoSignalAgent
        return NexoSignalAgent(["AAPL"], dry_run=dry_run, poll_interval_sec=1)


class TestDryRunNeverPlacesOrders(unittest.TestCase):
    """In dry-run mode, place_market_order and place_bracket_order must never be called."""

    def test_dry_run_does_not_call_place_market_order(self):
        broker = _make_mock_broker()
        agent = _make_agent(broker, dry_run=True)
        with (
            patch("trading_agent.agent.record_signal_event"),
            patch("trading_agent.agent.record_trade_event"),
            patch("trading_agent.agent.record_agent_event"),
            # Force strategy to emit a buy signal
            patch.object(agent.strategy, "signal", return_value="buy"),
        ):
            agent.evaluate_symbol("AAPL")
        broker.place_market_order.assert_not_called()

    def test_dry_run_does_not_call_place_bracket_order(self):
        broker = _make_mock_broker()
        agent = _make_agent(broker, dry_run=True)
        pick = MagicMock()
        pick.symbol = "AAPL"
        pick.price = 150.0
        pick.atr = 2.0
        pick.probability = 0.7
        pick.confluence_score = 80.0
        pick.order_book_imbalance = 0.0
        pick.slippage_estimate_bps = 0.0
        pick.liquidity_score = 70.0
        with (
            patch("trading_agent.storage._storage_enabled", return_value=False),
            patch("trading_agent.storage.record_signal_event"),
            patch("trading_agent.storage.record_trade_event"),
            patch("trading_agent.storage.record_agent_event"),
            patch("trading_agent.storage.record_risk_metrics"),
        ):
            agent.execute_alpha_pick(pick)
        broker.place_bracket_order.assert_not_called()


class TestCircuitBreaker(unittest.TestCase):
    """Circuit breaker blocks all trading when active."""

    def _agent_with_breaker(self):
        broker = _make_mock_broker()
        agent = _make_agent(broker, dry_run=False)
        return agent, broker

    def test_trip_sets_active(self):
        agent, broker = self._agent_with_breaker()
        agent.trip_circuit_breaker("test reason")
        self.assertTrue(agent.circuit_breaker_active)

    def test_tripped_breaker_cancels_orders(self):
        agent, broker = self._agent_with_breaker()
        agent.trip_circuit_breaker("test reason")
        broker.cancel_all_orders.assert_called_once()

    def test_evaluate_symbol_blocked_when_breaker_active(self):
        agent, broker = self._agent_with_breaker()
        agent.circuit_breaker_active = True
        result = agent.evaluate_symbol("AAPL")
        self.assertIsNone(result)
        broker.get_bars.assert_not_called()

    def test_two_strikes_trips_breaker(self):
        agent, broker = self._agent_with_breaker()
        self.assertFalse(agent.circuit_breaker_active)
        agent.add_strike("first infraction")
        self.assertFalse(agent.circuit_breaker_active)
        agent.add_strike("second infraction")
        self.assertTrue(agent.circuit_breaker_active)

    def test_reset_session_clears_breaker(self):
        agent, broker = self._agent_with_breaker()
        agent.circuit_breaker_active = True
        agent.strike_count = 2
        with patch("trading_agent.agent_jobs.emit_agent_event"):
            agent.reset_session()
        self.assertFalse(agent.circuit_breaker_active)
        self.assertEqual(agent.strike_count, 0)


class TestPerSymbolQuarantine(unittest.TestCase):
    """Symbols with repeated scan errors should be quarantined."""

    def test_quarantine_after_three_consecutive_errors(self):
        broker = _make_mock_broker()
        broker.get_bars.side_effect = Exception("network error")
        agent = _make_agent(broker, dry_run=True)

        with (
            patch("trading_agent.storage._storage_enabled", return_value=False),
            patch("trading_agent.storage.record_agent_event"),
        ):
            # Three consecutive failures → quarantined
            for _ in range(3):
                agent.run_once()

        self.assertIn("AAPL", agent._quarantined)

    def test_successful_eval_resets_error_count(self):
        broker = _make_mock_broker()
        agent = _make_agent(broker, dry_run=True)
        agent._symbol_error_counts["AAPL"] = 2

        with (
            patch.object(agent, "evaluate_symbol", return_value=None),
            patch("trading_agent.storage._storage_enabled", return_value=False),
            patch("trading_agent.storage.record_agent_event"),
        ):
            agent.run_once()

        self.assertEqual(agent._symbol_error_counts.get("AAPL", 0), 0)


if __name__ == "__main__":
    unittest.main()
