"""NexoSignal Agent realtime event bus.

Extracted from agent.py so both the core agent and the jobs mixin can emit
events without creating a circular import.
"""

from __future__ import annotations

import logging
from typing import Callable

logger = logging.getLogger("trading_agent")

_event_listeners: list[Callable] = []


def register_agent_event_listener(listener: Callable) -> None:
    """Register a callable to receive (event_type, payload) pairs."""
    _event_listeners.append(listener)


def emit_agent_event(event_type: str, payload: dict) -> None:
    """Broadcast an event to all registered listeners."""
    for listener in list(_event_listeners):
        try:
            listener(event_type, payload)
        except Exception:
            logger.exception("NexoSignal Agent realtime listener failed")
