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

The dashboard shows:

- Portfolio, cash, buying power, positions, and open orders
- Manual order form with dry-run support
- Bot start/stop controls
- AlphaCore signal analytics
- NexoSignal Intelligence Panel
- Trade Ledger events
- Signal events
- Telemetry events
- MetaMask public wallet capture
- Brokerage connection records

For first-time testing:

1. Confirm `TRADING_MODE=paper`.
2. Start the bot with `Dry run` checked.
3. Watch `trade_events`, `signal_events`, and `performance_events`.
4. Review dry-run behavior before submitting live orders.

## 8. Run A Dry-Run Report

```powershell
python scripts/dry_run_report.py
```

This summarizes recent signal approvals, trade captures, confidence, and dry-run activity.

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
- Crypto bracket execution is intentionally blocked in the safe initial rollout until Alpaca crypto order behavior is validated in paper mode.
- Never commit `.env` or API keys.

## Useful Commands

```powershell
# Run dashboard
python dashboard.py

# Compile-check Python files
python -m py_compile dashboard.py main.py trading_agent\agent.py trading_agent\broker.py trading_agent\config.py trading_agent\signal_engine.py trading_agent\storage.py trading_agent\strategy.py

# Generate dry-run report
python scripts\dry_run_report.py
```
