import json as _json
import os
from dotenv import load_dotenv

load_dotenv(override=True)

ALPACA_API_KEY    = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
TRADING_MODE      = os.getenv("TRADING_MODE", "paper").lower()  # "paper" or "live"
LIVE_TRADING      = os.getenv("LIVE_TRADING", "false").lower() in {"true", "1", "yes"}
AUTONOMOUS_TRADING = os.getenv("AUTONOMOUS_TRADING", "false").lower() in {"true", "1", "yes"}
REQUIRE_MANUAL_APPROVAL = os.getenv("REQUIRE_MANUAL_APPROVAL", "true").lower() not in {"false", "0", "no"}
ALPACA_PAPER = os.getenv("ALPACA_PAPER", "true").lower() not in {"false", "0", "no"}

# Base URLs
PAPER_BASE_URL = "https://paper-api.alpaca.markets"
LIVE_BASE_URL  = "https://api.alpaca.markets"
BASE_URL = PAPER_BASE_URL if TRADING_MODE == "paper" else LIVE_BASE_URL

# Safety limits
MAX_POSITION_SIZE_PCT  = float(os.getenv("MAX_POSITION_SIZE_PCT", "0.10"))
DAILY_LOSS_LIMIT_PCT   = float(os.getenv("DAILY_LOSS_LIMIT_PCT", "0.05"))
MAX_TRADES_PER_DAY     = int(os.getenv("MAX_TRADES_PER_DAY", "20"))
MAX_ORDER_VALUE_USD    = float(os.getenv("MAX_ORDER_VALUE_USD", "1000"))
MIN_CASH_RESERVE_USD   = float(os.getenv("MIN_CASH_RESERVE_USD", "500"))
POSITION_SIZE_PCT      = float(os.getenv("POSITION_SIZE_PCT", "0.10"))
MAX_DAILY_LOSS_LIMIT   = float(os.getenv("MAX_DAILY_LOSS_LIMIT", "500"))
MAX_POSITION_SIZE      = float(os.getenv("MAX_POSITION_SIZE", "5000"))
MIN_RISK_REWARD_RATIO  = float(os.getenv("MIN_RISK_REWARD_RATIO", "5.0"))
MAX_DATA_STALENESS_SECONDS = int(os.getenv("MAX_DATA_STALENESS_SECONDS", "900"))
MAX_GUARD_STRIKES      = int(os.getenv("MAX_GUARD_STRIKES", "2"))

_whitelist_raw = os.getenv("SYMBOL_WHITELIST", "")
_blacklist_raw = os.getenv("SYMBOL_BLACKLIST", "")
SYMBOL_WHITELIST = set(s.strip().upper() for s in _whitelist_raw.split(",") if s.strip())
SYMBOL_BLACKLIST = set(s.strip().upper() for s in _blacklist_raw.split(",") if s.strip())

# Dashboard
DASHBOARD_BRAND_NAME = os.getenv("DASHBOARD_BRAND_NAME", "NexoSignal")
DASHBOARD_USERNAME = os.getenv("DASHBOARD_USERNAME", "admin")
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "change-me")
FLASK_SECRET_KEY = os.getenv("FLASK_SECRET_KEY", "change-this-secret-key")

# Multi-user support: DASHBOARD_USERS overrides the single-user vars above.
# Format: JSON object of { "username": "password_or_hash", ... }
# Example: DASHBOARD_USERS={"admin":"secret123","trader":"pass456"}
# Falls back to DASHBOARD_USERNAME / DASHBOARD_PASSWORD if not set.
_users_raw = os.getenv("DASHBOARD_USERS", "")
if _users_raw:
    try:
        DASHBOARD_USERS: dict[str, str] = _json.loads(_users_raw)
    except Exception:
        DASHBOARD_USERS = {DASHBOARD_USERNAME: DASHBOARD_PASSWORD}
else:
    DASHBOARD_USERS = {DASHBOARD_USERNAME: DASHBOARD_PASSWORD}
SUPABASE_DB_URL = os.getenv("SUPABASE_DB_URL", "")
SUPABASE_PROJECT_URL = os.getenv("SUPABASE_PROJECT_URL", "")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "")
DATABASE_LABEL = "POSTGRES"
METAMASK_REQUIRED = os.getenv("METAMASK_REQUIRED", "false").lower() in {"true", "1", "yes"}
RUNNING_ON_VERCEL = os.getenv("VERCEL", "0") == "1"
SIGNAL_MIN_CONFIDENCE = float(os.getenv("SIGNAL_MIN_CONFIDENCE", "70"))
DEFAULT_DASHBOARD_SYMBOLS = [
    s.strip().upper()
    for s in os.getenv("DEFAULT_DASHBOARD_SYMBOLS", "AAPL,MSFT,SPY,QQQ,BTC/USD,ETH/USD").split(",")
    if s.strip()
]

# Multi-agent AI Research Layer
# Gemini: market research for stocks/crypto/ETFs/Bitcoin
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
# Grok (xAI): news sentiment and short-term price prediction
GROK_API_KEY = os.getenv("GROK_API_KEY", "")
# Claude (Anthropic): final trade execution with 5:1 risk-reward validation
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
# Enable AI research layer (requires at least one API key)
AI_RESEARCH_ENABLED = os.getenv("AI_RESEARCH_ENABLED", "false").lower() in {"true", "1", "yes"}
# Minimum risk:reward ratio Claude must confirm before approving a trade
AI_MIN_RISK_REWARD_RATIO = float(os.getenv("AI_MIN_RISK_REWARD_RATIO", "5.0"))
# Master Decision consensus floor. Lower for research, higher for live automation.
MASTER_CONSENSUS_MIN_CONFIDENCE = float(os.getenv("MASTER_CONSENSUS_MIN_CONFIDENCE", "70"))
# Optional local Ollama/Mistral analyst. Disabled by default.
LOCAL_LLM_ENABLED = os.getenv("LOCAL_LLM_ENABLED", "false").lower() in {"true", "1", "yes"}
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "mistral")

# NexoSignal mobile alerts
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# NexoSignal AlphaCore model/cache
ALPHACORE_MODEL_PATH = os.getenv("ALPHACORE_MODEL_PATH", "alphacore_model.pkl")

# NexoSignal Phase 2 — Intelligence Extension
FMP_API_KEY       = os.getenv("FMP_API_KEY", "")         # Financial Modeling Prep (free: 250 calls/day)
NEWS_API_KEY      = os.getenv("NEWS_API_KEY", "")         # NewsAPI.org (free tier)
FRED_API_KEY      = os.getenv("FRED_API_KEY", "")         # FRED (St. Louis Fed, free)
MAX_PORTFOLIO_VAR = float(os.getenv("MAX_PORTFOLIO_VAR", "0.02"))   # 2 % daily portfolio VaR hard limit
WATCHLIST_SIZE    = int(os.getenv("WATCHLIST_SIZE", "30"))           # Scout: symbols to maintain
FINBERT_MODEL_PATH = os.getenv("FINBERT_MODEL_PATH", "ProsusAI/finbert")  # HuggingFace FinBERT
MARKET_DATA_PROVIDER = os.getenv("MARKET_DATA_PROVIDER", "alpaca,yahoo,tradingview").lower()
TRADINGVIEW_WEBHOOK_SECRET = os.getenv("TRADINGVIEW_WEBHOOK_SECRET", "")

# Phase C — Dev mode / structured logging
# Set STRUCTURED_LOG_FILE to a path (e.g. "nexosignal.jsonl") to enable
# one-JSON-object-per-line structured log output alongside the normal log.
STRUCTURED_LOG_FILE = os.getenv("STRUCTURED_LOG_FILE", "")
