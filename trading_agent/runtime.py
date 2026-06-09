"""Unified NexoSignal runtime lifecycle.

AgentRuntime owns the scan loop thread and the APScheduler jobs for a single
NexoSignalAgent instance. Dashboard, CLI, and module entrypoints should use this
instead of starting ``agent.run()`` directly.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from .agent import NexoSignalAgent
from .scheduler import start_nexosignal_scheduler

logger = logging.getLogger("trading_agent.runtime")


class AgentRuntime:
    def __init__(self, agent: NexoSignalAgent):
        self.agent = agent
        self._thread: threading.Thread | None = None
        self._scheduler: Any | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            if self.is_running:
                return
            self.agent._stop_event.clear()
            self._thread = threading.Thread(
                target=self.agent.run,
                name="nexosignal-scan-loop",
                daemon=True,
            )
            self._thread.start()
            try:
                self._scheduler = start_nexosignal_scheduler(self.agent)
            except Exception:
                self.agent.stop()
                logger.exception("NexoSignal scheduler failed to start")
                raise
            self.agent._log("NexoSignal AgentRuntime started", event_type="runtime_started")

    def stop(self, timeout: float = 10.0) -> None:
        with self._lock:
            self.agent.stop()
            if self._scheduler is not None:
                try:
                    self._scheduler.shutdown(wait=False)
                except Exception:
                    logger.exception("NexoSignal scheduler shutdown failed")
                finally:
                    self._scheduler = None
            thread = self._thread

        if thread and thread.is_alive():
            thread.join(timeout=timeout)

        with self._lock:
            if self._thread and not self._thread.is_alive():
                self._thread = None
            self.agent._log("NexoSignal AgentRuntime stopped", event_type="runtime_stopped")

    @property
    def is_running(self) -> bool:
        thread_alive = bool(self._thread and self._thread.is_alive())
        return bool(self.agent._running and thread_alive)

    @property
    def scheduler_running(self) -> bool:
        return bool(self._scheduler and getattr(self._scheduler, "running", False))

    def scheduler_jobs(self) -> list[dict[str, Any]]:
        if not self._scheduler:
            return []
        jobs = []
        for job in self._scheduler.get_jobs():
            jobs.append({
                "id": job.id,
                "name": job.name,
                "next_run_time": job.next_run_time.isoformat() if job.next_run_time else None,
            })
        return jobs

    def status(self) -> dict[str, Any]:
        status = self.agent.status()
        status.update({
            "running": self.is_running,
            "configured": True,
            "scheduler_running": self.scheduler_running,
            "scheduler_jobs": self.scheduler_jobs(),
            "symbols": self.agent.symbols,
            "strategy": self.agent.strategy.name,
            "qty": self.agent.qty_per_trade,
            "interval": self.agent.poll_interval,
            "dry_run": self.agent.dry_run,
            "log": self.agent.log[-25:],
            "errors": self.agent.errors[-10:],
        })
        return status
