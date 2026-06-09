"""Centralised feature flags for NexoSignal.

All flags are evaluated once at import time from config so callers never need
to reach into config directly for capability checks.

Usage::

    from trading_agent.flags import AI_RESEARCH, STORAGE, SCHEDULER
    if STORAGE:
        record_trade_event(...)
"""

from __future__ import annotations

from . import config as _cfg

# ── AI / LLM ─────────────────────────────────────────────────────────────────

AI_RESEARCH: bool = _cfg.AI_RESEARCH_ENABLED
"""Gemini + Grok + Claude research panel is enabled."""

LOCAL_LLM_MASTER: bool = _cfg.LOCAL_LLM_ENABLED
"""Local Ollama master orchestrator is active."""

# ── Infrastructure ────────────────────────────────────────────────────────────

STORAGE: bool = bool(_cfg.SUPABASE_DB_URL)
"""Supabase/Postgres storage is available."""

SCHEDULER: bool = not _cfg.RUNNING_ON_VERCEL
"""APScheduler background jobs can run (not on Vercel serverless)."""

TELEGRAM: bool = bool(_cfg.TELEGRAM_TOKEN and _cfg.TELEGRAM_CHAT_ID)
"""Telegram alert channel is configured."""

# ── Intelligence Extension (Phase 2) ─────────────────────────────────────────

SCOUT: bool = bool(_cfg.FMP_API_KEY)
"""NexoSignal Scout (FMP watchlist rebuild) is enabled."""

MACRO: bool = bool(_cfg.FRED_API_KEY)
"""NexoSignal Lens macro regime detection (FRED) is enabled."""

INSIDER_PARSING: bool = True
"""SEC Form 4 insider parsing is always attempted when watchlist is populated."""
