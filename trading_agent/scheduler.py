"""APScheduler configuration for NexoSignal.

Extracted from agent.py so the scheduler wiring lives separately from the
agent scan-loop logic.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .agent import NexoSignalAgent

try:
    from apscheduler.schedulers.background import BackgroundScheduler
except Exception:  # pragma: no cover
    BackgroundScheduler = None  # type: ignore[assignment,misc]

import pytz

logger = logging.getLogger("trading_agent.scheduler")

_EASTERN = pytz.timezone("America/New_York")


def start_nexosignal_scheduler(agent: "NexoSignalAgent"):
    """Attach and start all NexoSignal cron/interval jobs on a BackgroundScheduler.

    Returns the running scheduler so the caller can shut it down cleanly.
    """
    if BackgroundScheduler is None:
        raise RuntimeError(
            "APScheduler is not installed. Run: pip install apscheduler"
        )

    scheduler = BackgroundScheduler(timezone=_EASTERN)

    # ── Core market jobs ──────────────────────────────────────────────────────
    scheduler.add_job(
        agent.run_market_open_scan,
        "cron",
        day_of_week="mon-fri",
        hour=9,
        minute=30,
        id="nexosignal_market_open",
    )
    scheduler.add_job(
        agent.heartbeat,
        "interval",
        seconds=60,
        id="nexosignal_heartbeat",
    )
    scheduler.add_job(
        agent.end_of_day_report,
        "cron",
        day_of_week="mon-fri",
        hour=16,
        minute=0,
        id="nexosignal_eod",
    )

    # ── Phase 2: Intelligence Extension jobs ─────────────────────────────────
    scheduler.add_job(
        agent.scout_rebuild,
        "cron",
        day_of_week="sat",
        hour=8,
        minute=0,
        id="nexosignal_scout_rebuild",
    )
    scheduler.add_job(
        agent.macro_refresh,
        "cron",
        day_of_week="sun",
        hour=8,
        minute=0,
        id="nexosignal_macro_refresh",
    )
    scheduler.add_job(
        agent.premarket_briefing,
        "cron",
        day_of_week="mon-fri",
        hour=7,
        minute=0,
        id="nexosignal_premarket",
    )
    scheduler.add_job(
        agent.insider_parse,
        "cron",
        day_of_week="mon-fri",
        hour=18,
        minute=0,
        id="nexosignal_insider_parse",
    )

    scheduler.start()
    agent._log(
        "NexoSignal scheduler started (market-open, heartbeat, EOD, Scout, Lens)",
        event_type="scheduler_started",
    )
    return scheduler
