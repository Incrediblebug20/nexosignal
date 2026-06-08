import os
from dotenv import load_dotenv

load_dotenv(override=True)

ALPACA_API_KEY    = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
TRADING_MODE      = os.getenv("TRADING_MODE", "paper").lower()  # "paper" or "live"

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

_whitelist_raw = os.getenv("SYMBOL_WHITELIST", "")
_blacklist_raw = os.getenv("SYMBOL_BLACKLIST", "")
SYMBOL_WHITELIST = set(s.strip().upper() for s in _whitelist_raw.split(",") if s.strip())
SYMBOL_BLACKLIST = set(s.strip().upper() for s in _blacklist_raw.split(",") if s.strip())

# Dashboard
DASHBOARD_BRAND_NAME = os.getenv("DASHBOARD_BRAND_NAME", "NexoSignal")
DASHBOARD_USERNAME = os.getenv("DASHBOARD_USERNAME", "admin")
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "change-me")
FLASK_SECRET_KEY = os.getenv("FLASK_SECRET_KEY", "change-this-secret-key")
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

# NexoSignal mobile alerts
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# NexoSignal AlphaCore model/cache
ALPHACORE_MODEL_PATH = os.getenv("ALPHACORE_MODEL_PATH", "alphacore_model.pkl")
