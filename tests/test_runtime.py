"""Integration tests: AgentRuntime start/stop lifecycle."""

from __future__ import annotations

import time
import threading
import unittest
from unittest.mock import MagicMock, patch


def _make_mock_broker():
    broker = MagicMock()
    broker._base = "https://paper-api.alpaca.markets"
    broker.is_market_open.return_value = False
    broker.get_account.return_value = {
        "portfolio_value": "100000",
        "cash": "50000",
        "buying_power": "50000",
    }
    broker.get_clock.return_value = {"is_open": False}
    broker.get_positions.return_value = []
    return broker


class TestAgentRuntimeLifecycle(unittest.TestCase):
    """AgentRuntime start → running → stop lifecycle."""

    def _make_agent(self, mock_broker):
        with (
            patch("trading_agent.agent.AlpacaBroker", return_value=mock_broker),
            patch("trading_agent.agent.TelegramNotifier", return_value=MagicMock()),
            patch("trading_agent.storage.init_db"),
            patch("trading_agent.storage._storage_enabled", return_value=False),
        ):
            from trading_agent.agent import NexoSignalAgent
            return NexoSignalAgent(["AAPL"], dry_run=True, poll_interval_sec=1)

    def test_start_sets_running(self):
        broker = _make_mock_broker()
        agent = self._make_agent(broker)
        thread = threading.Thread(target=agent.run, daemon=True)
        thread.start()
        time.sleep(0.1)
        self.assertTrue(agent._running)
        agent.stop()
        thread.join(timeout=3)
        self.assertFalse(agent._running)

    def test_stop_signals_event(self):
        broker = _make_mock_broker()
        agent = self._make_agent(broker)
        self.assertFalse(agent._stop_event.is_set())
        agent.stop()
        self.assertTrue(agent._stop_event.is_set())

    def test_double_stop_is_idempotent(self):
        broker = _make_mock_broker()
        agent = self._make_agent(broker)
        agent.stop()
        agent.stop()  # should not raise
        self.assertTrue(agent._stop_event.is_set())

    def test_initial_state(self):
        broker = _make_mock_broker()
        agent = self._make_agent(broker)
        self.assertFalse(agent._running)
        self.assertFalse(agent.circuit_breaker_active)
        self.assertEqual(agent.strike_count, 0)
        self.assertEqual(agent._quarantined, set())
        self.assertEqual(agent._symbol_error_counts, {})


class TestAgentRuntimeStatus(unittest.TestCase):
    """Agent.status() returns the expected shape."""

    def _make_agent(self, mock_broker):
        with (
            patch("trading_agent.agent.AlpacaBroker", return_value=mock_broker),
            patch("trading_agent.agent.TelegramNotifier", return_value=MagicMock()),
            patch("trading_agent.storage._storage_enabled", return_value=False),
        ):
            from trading_agent.agent import NexoSignalAgent
            return NexoSignalAgent(["SPY", "QQQ"], dry_run=True)

    def test_status_keys_present(self):
        broker = _make_mock_broker()
        agent = self._make_agent(broker)
        status = agent.status()
        for key in ("mode", "dry_run", "market_open", "portfolio_value", "cash",
                    "strategy", "watching", "circuit_breaker_active", "quarantined_symbols"):
            self.assertIn(key, status, f"Missing key: {key}")

    def test_status_dry_run_true(self):
        broker = _make_mock_broker()
        agent = self._make_agent(broker)
        self.assertTrue(agent.status()["dry_run"])

    def test_status_watching_normalised(self):
        broker = _make_mock_broker()
        agent = self._make_agent(broker)
        self.assertIn("SPY", agent.status()["watching"])


if __name__ == "__main__":
    unittest.main()
