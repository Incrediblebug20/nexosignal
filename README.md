# NexoSignal

NexoSignal is a Flask-based trading dashboard and autonomous trading agent for Alpaca paper/live trading. It stores trade, signal, intelligence, risk, and telemetry events in Supabase/Postgres.

The system is organized into these layers:

- AlphaCore: technical signal scoring and confluence ranking
- Guard: risk limits, VaR checks, cash reserve, and execution blocks
- Executor: Alpaca order submission and bracket orders
- Ledger: Supabase/Postgres persistence
- Agent: background scan and scheduler loop
- Scout: watchlist ranking
- Lens: macro, insider, earnings, and AI-assisted research

Start in paper trading and dry-run mode. Do not enable live trading until you have reviewed dry-run results across multiple market sessions.

## Requirements

- Python 3.12+
- Alpaca account and paper API keys
- Supabase project with a Postgres connection string
- Git
- Optional: Telegram bot token/chat ID for alerts
- Optional: Gemini, Grok/xAI, Anthropic, FMP, FRED, NewsAPI keys

## 1. Clone The Repo

```powershell
git clone https://github.com/Incrediblebug20/nexosignal.git
cd nexosignal
```

## 2. Create A Virtual Environment

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

If PowerShell blocks activation, run:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

Then activate again.

## 3. Install Dependencies

```powershell
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

## 4. Configure Environment Variables

Copy the example file:

```powershell
Copy-Item .env.example .env
```

Edit `.env` and set at minimum:

```env
ALPACA_API_KEY=your_alpaca_paper_key
ALPACA_SECRET_KEY=your_alpaca_paper_secret
TRADING_MODE=paper

DASHBOARD_USERNAME=admin
DASHBOARD_PASSWORD=change-this-password
FLASK_SECRET_KEY=replace-with-a-long-random-string

SUPABASE_DB_URL=postgresql://postgres:PASSWORD@HOST:5432/postgres?sslmode=require
SUPABASE_PROJECT_URL=https://your-project.supabase.co
SUPABASE_ANON_KEY=your_supabase_anon_key
```

Keep `.env` private. It is ignored by git and should never be committed.

## 5. Create Supabase Tables

The app calls `init_db()` on startup and creates required tables automatically when `SUPABASE_DB_URL` is valid.

You can also create the tables manually:

1. Open your Supabase project.
2. Go to SQL Editor.
3. Run the contents of `supabase_schema.sql`.
4. Keep Row Level Security enabled.

## 6. Run The Dashboard Locally

```powershell
python dashboard.py
```

Open:

```text
http://127.0.0.1:5000/login
```

Log in with:

```env
DASHBOARD_USERNAME
DASHBOARD_PASSWORD
```

If `DASHBOARD_USERS` is set, it overrides the single-user login. Example:

```env
DASHBOARD_USERS={"admin":"secret123","trader":"pass456"}
```

## 7. Use The Dashboard

### Sidebar navigation

The sidebar gives one-click access to:

| Page | Route | Description |
|---|---|---|
| Portfolio | `/` | Account, positions, open orders, trade ledger |
| Markets | `/markets` | US equities, Asian indices, crypto, screeners |
| Charts | `/chart/<symbol>` | TradingView candlestick + indicator + signal overlay |
| Signals | `/portfolio#signals` | AlphaCore signal feed |
| Research | `/research` | Multi-agent AI research (Gemini/Grok/Claude/Local LLM) |
| Trade | `/trade` | Manual orders, bot start/stop, dry-run mode |

Collapse the sidebar with the `«` toggle. On mobile, tap the hamburger (☰) to open it as an overlay.

### Interactive charts

Navigate to `/chart/AAPL` (or any symbol) for:

- **Candlestick + volume** histogram at 5m / 15m / 1H / 1D / 1W
- **Indicator overlays**: SMA 20, SMA 50, EMA 9 (toggleable)
- **RSI 14** sub-chart synchronized with the main time scale
- **AlphaCore signal** pill (BUY / SELL / HOLD + confidence %)
- **Auto-refresh** — latest bar updates every 60 s without re-rendering
- **Signal history** table for the selected symbol
- Press `/` to focus the symbol search box; Enter navigates to the new chart

### Starting the bot

For first-time testing:

1. Confirm `TRADING_MODE=paper`.
2. Start the bot with `Dry run` checked.
3. Watch `trade_events`, `signal_events`, and `performance_events`.
4. Review dry-run behavior before submitting live orders.

## 8. Run A Dry-Run Report

```powershell
python scripts/dry_run_report.py
```

This summarizes recent signal approvals, trade captures, confidence, Top 3 predictions, Top 5 Alpha Picks, 5:1 risk-reward levels, and dry-run activity.

## 8.1 Dashboard Intelligence APIs

All API routes require a valid session cookie (login first). All write routes are POST/PATCH only.

**Core signals & positions**

| Method | Route | Description |
|---|---|---|
| GET | `/health` | Service health + mode |
| GET | `/api/nexosignal/status` | Agent status + safety config |
| GET | `/api/nexosignal/telemetry` | Full telemetry snapshot |
| GET | `/api/nexosignal/top-predictions` | Top 3 AlphaCore predictions |
| GET | `/api/nexosignal/alpha-picks` | Top 5 alpha picks |
| GET | `/api/nexosignal/risk-reward/<sym>` | Risk/reward analysis for symbol |
| GET | `/api/nexosignal/history/<sym>` | OHLCV + indicators (dashboard use) |
| GET | `/api/nexosignal/strategy-performance` | Per-strategy trade statistics |
| GET | `/api/nexosignal/positions` | Live Alpaca positions |
| GET | `/api/nexosignal/pnl` | P&L snapshot |
| POST | `/api/nexosignal/alerts/test` | Send test Telegram alert |

**Chart page APIs**

| Method | Route | Notes |
|---|---|---|
| GET | `/api/chart/<sym>/ohlcv` | OHLCV + SMA20/50/EMA9/RSI14 overlays; `?timeframe=1Day&limit=200`; cached 30–300 s by timeframe |
| GET | `/api/chart/<sym>/signals` | Signal history filtered to one symbol |

**Market data & research**

| Method | Route | Description |
|---|---|---|
| GET | `/api/ticker-strip` | 14-symbol price ticker (cached 30 s) |
| GET | `/api/market-data` | US/Asia/crypto tables; `?region=us&screen=active` |
| GET | `/api/research/earnings` | FMP earnings calendar |
| GET | `/api/research/sentiment/<sym>` | StockTwits bull/bear sentiment |
| GET | `/api/research/dcf/<sym>` | FMP DCF fair value |
| GET | `/api/research/analyst/<sym>` | FMP analyst upgrades/downgrades |
| GET | `/api/research/patterns/<sym>` | Candlestick patterns (requires pandas-ta) |
| GET | `/api/research/sector-allocation` | Portfolio sector breakdown |
| GET | `/api/ai-research/<sym>` | Multi-agent AI research (requires keys) |
| GET | `/api/export/bars/<sym>` | CSV OHLCV download |

**Webhooks**

| Method | Route | Description |
|---|---|---|
| POST | `/webhooks/tradingview` | Receive TradingView alerts; optional `TRADINGVIEW_WEBHOOK_SECRET` |

Top predictions are probabilistic rankings, not guaranteed outcomes. AI/ML research can inform ranking, but it must not bypass AlphaCore, Guard, or risk checks.

## 9. Optional Telegram Alerts

Create a Telegram bot with BotFather, get your chat ID, then set:

```env
TELEGRAM_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
```

NexoSignal sends alerts for picks, bracket order acknowledgments, stop adjustments, circuit breakers, and end-of-day reports.

## 10. Optional AI And Intelligence Keys

These are optional:

```env
GEMINI_API_KEY=
GROK_API_KEY=
ANTHROPIC_API_KEY=
AI_RESEARCH_ENABLED=false

FMP_API_KEY=
FRED_API_KEY=
NEWS_API_KEY=
```

The AI layer is advisory. It should not bypass AlphaCore, Guard, or dry-run validation.

## 11. Vercel Deployment

Vercel can host the web dashboard through:

- `api/index.py`
- `vercel.json`
- `.env.vercel.example`

Set the same environment variables in Vercel.

Important: Vercel serverless functions should not run the continuous trading loop. Use Vercel for the dashboard only. Run the bot locally, on a VPS, or on a persistent worker host.

## Documentation

- `PROJECT_STRUCTURE_ANALYSIS.md` — architecture layers, module map, data flow, and entry points
- `ARCHITECTURE_TREE.md` — folder tree and cleanup notes
- `DASHBOARD_README.md` — dashboard quickstart
- `VERCEL_DEPLOYMENT.md` — Vercel + Supabase deployment
- `PROJECT_SUMMARY_REPORT.md` — detailed feature summary

## Safety Notes

- Start with Alpaca paper trading.
- Keep dry-run enabled until results are reviewed.
- NexoSignal does not guarantee profitable trades.
- Live autonomous orders are disabled by default with `LIVE_TRADING=false`, `AUTONOMOUS_TRADING=false`, `REQUIRE_MANUAL_APPROVAL=true`, and `ALPACA_PAPER=true`.
- Autonomous candidates must pass confluence, ATR, liquidity, fresh-data, buying-power, circuit-breaker, and 5:1 risk-reward checks.
- The app has no withdrawal, transfer, ACH, wire, card, bridge, or external fund movement logic.
- Crypto bracket execution is intentionally blocked in the safe initial rollout until Alpaca crypto order behavior is validated in paper mode.
- Never commit `.env` or API keys.

## Useful Commands

```powershell
# Run dashboard
python dashboard.py

# Compile-check Python files
python -m py_compile dashboard.py main.py trading_agent\agent.py trading_agent\broker.py trading_agent\config.py trading_agent\restrictions.py trading_agent\signal_engine.py trading_agent\storage.py trading_agent\strategy.py

# Generate dry-run report
python scripts\dry_run_report.py
```
