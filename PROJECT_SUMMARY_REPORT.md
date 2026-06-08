# NexoSignal - Project Summary Report

Updated: June 8, 2026

## Current State

NexoSignal is now a Flask-based trading operations dashboard and bot system for
Alpaca paper/live trading, Supabase/Postgres storage, MetaMask public wallet
capture, AI-assisted research, signal scoring, risk controls, and telemetry.

The project is branded as:

```text
NexoSignal
```

The current implementation is suitable for local dashboard use, paper-trading
dry runs, Supabase-backed audit logs, and Vercel dashboard deployment. Live
trading should remain disabled until dry-run results have been reviewed over
multiple market sessions.

## Architecture

```text
Browser dashboard
-> Flask app in dashboard.py
-> NexoSignal Agent / AlpacaBroker / Signal Engine
-> Alpaca APIs for account, orders, market data
-> Supabase/Postgres for trade, signal, intelligence, and telemetry records
```

Main folders:

- `dashboard.py` - Flask routes, login, dashboard rendering, JSON APIs
- `trading_agent/agent.py` - bot loop, scheduler, AlphaCore execution flow
- `trading_agent/broker.py` - Alpaca account/order/market-data wrapper
- `trading_agent/signal_engine.py` - AlphaCore scoring and indicators
- `trading_agent/ai_research.py` - Gemini, Grok, Claude, optional Ollama layer
- `trading_agent/storage.py` - Supabase/Postgres persistence
- `trading_agent/strategy.py` - SMA, RSI, VWAP, Scout/Lens/Guard helpers
- `templates/` - dashboard/login HTML
- `static/` - dashboard JavaScript, MetaMask integration, CSS
- `api/index.py` - Vercel serverless entrypoint
- `supabase_schema.sql` - manual Supabase schema setup

## Dashboard Features

The dashboard is login-protected and shows one operations console for the bot.

It includes:

- Market, bot, and database status chips
- Portfolio, cash, buying power, trade count, signal count, approval metrics
- AlphaCore analytics charts
- AI research console
- Manual order form with dry-run support
- Bot start/stop controls
- Autonomous trade tracker
- NexoSignal Intelligence Panel
- Open positions
- Open orders with cancel action
- Guardrail summary
- MetaMask public wallet capture
- Brokerage connection registry
- AlphaCore signal/event history
- Ledger trade history
- Agent log

## How The Dashboard Works

1. Login

   The user signs in using `DASHBOARD_USERNAME` and `DASHBOARD_PASSWORD` from
   `.env`. The app uses a Flask session to protect all dashboard routes.

2. Startup

   On import/startup, `dashboard.py` calls `init_db()` from
   `trading_agent/storage.py`. This checks the Supabase/Postgres connection and
   creates required tables if they do not already exist.

3. Account Snapshot

   The dashboard creates an `AlpacaBroker` instance and fetches:

   - account details
   - market clock
   - positions
   - open orders

   If Alpaca credentials are invalid, the dashboard stays online but shows a
   friendly connection error.

4. Manual Orders

   The manual order form can record dry-run orders or submit Alpaca market
   orders. Every attempt is written to `trade_events`.

5. Bot Control

   The Bot Control panel starts a local background thread running
   `NexoSignalAgent`. The agent scans configured symbols at the selected
   interval, applies the selected strategy, scores the signal, and either holds,
   records a dry-run event, or submits an order.

   Vercel does not run the continuous bot loop. Vercel is only for the online
   dashboard. The bot loop should run locally, on a VPS, or on a persistent
   worker.

6. Signal Flow

   Each symbol goes through:

   ```text
   Alpaca bars
   -> selected strategy
   -> NexoSignal AlphaCore indicators and confidence score
   -> risk/restriction checks
   -> dry-run record or Alpaca order
   -> Supabase/Postgres audit event
   ```

7. Live Refresh And APIs

   The frontend JavaScript calls JSON endpoints such as:

   - `/api/live-metrics`
   - `/api/chart-data`
   - `/api/intelligence`
   - `/api/performance`
   - `/api/ai-research/<symbol>`

   These endpoints power charts, intelligence widgets, AI research cards, and
   telemetry tables.

## NexoSignal AlphaCore

Implemented in `trading_agent/signal_engine.py`.

AlphaCore now scores signals with:

- SMA fast/slow
- EMA20
- EMA50
- RSI
- MACD
- MACD signal
- MACD histogram
- VWAP
- ATR
- Volume ratio
- Volume z-score
- Candle direction
- Candle body strength
- Trend classification
- Order book imbalance
- Spread bps
- Estimated slippage bps
- Liquidity score

AlphaCore candidate ranking now includes a liquidity gate. Candidates with weak
liquidity are filtered before they reach execution.

Approval threshold:

```env
SIGNAL_MIN_CONFIDENCE=70
```

## NexoSignal Master Decision

Implemented in `trading_agent/ai_research.py`.

The AI research layer now has a Master Decision object that consolidates:

- Gemini market research
- Grok news/sentiment research
- Claude execution validation
- Optional local Ollama/Mistral research
- Base technical signal confidence

The Master Decision forces `HOLD` when:

- agents disagree,
- consensus confidence is below the configured threshold,
- Claude does not approve the required risk/reward setup.

Configuration:

```env
MASTER_CONSENSUS_MIN_CONFIDENCE=70
AI_MIN_RISK_REWARD_RATIO=5.0
LOCAL_LLM_ENABLED=false
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=mistral
```

## AI Research Layer

Implemented in `trading_agent/ai_research.py`.

Provider roles:

- Gemini - market, fundamental, and technical research
- Grok/xAI - news, sentiment, and short-term catalyst analysis
- Claude/Anthropic - final 5:1 risk/reward validation
- Ollama/Mistral - optional local analyst for no-cloud dry-run research

This layer is advisory. It must not bypass AlphaCore, Guard, or dry-run
validation.

## NexoSignal Guard

Risk and safety controls include:

- Paper trading default
- Dry-run order capture
- Symbol whitelist/blacklist
- Maximum order value
- Maximum position size
- Minimum cash reserve
- Maximum trades per day
- Daily loss limit
- Portfolio VaR storage
- Correlation flag storage
- Circuit breaker state in the agent

The dashboard should be used in dry-run mode first.

## NexoSignal Scout And Lens

Scout/Lens additions provide broader market intelligence.

Scout:

- Watchlist table
- Composite scoring fields
- Scheduled watchlist rebuild support

Lens:

- FRED macro regime snapshots
- SEC Form 4 insider activity parsing
- Earnings quality analysis storage
- Annual report analysis storage
- Sentiment scoring fallback

Related dashboard endpoints:

- `/api/intelligence`
- `/api/watchlist`
- `/api/macro`
- `/api/insiders`

## NexoSignal Telemetry

Telemetry was added to support dry-run efficiency testing.

Stored in `performance_events`:

- stage
- symbol
- latency in milliseconds
- slippage estimate in bps
- mark-to-market PnL
- realized PnL placeholder
- win-rate/acceptance-rate snapshot
- trade count
- uptime seconds
- raw payload JSON

Dashboard endpoint:

```text
/api/performance
```

Important: current win rate is an operational acceptance-rate proxy until
closed-trade outcome reconciliation is added.

## Storage

Storage is Supabase/Postgres only.

Required:

```env
SUPABASE_DB_URL=...
SUPABASE_PROJECT_URL=...
SUPABASE_ANON_KEY=...
```

Current tables:

- `trade_events`
- `signal_events`
- `agent_events`
- `wallet_connections`
- `brokerage_connections`
- `watchlist`
- `macro_snapshots`
- `insider_activity`
- `earnings_analysis`
- `annual_report_analysis`
- `risk_metrics`
- `performance_events`

RLS is enabled in `supabase_schema.sql`. The Flask server writes with the direct
Postgres connection string.

## Brokerage Integrations

Implemented for live API use:

- Alpaca

Tracked as dashboard connection records:

- Alpaca
- Interactive Brokers
- Charles Schwab
- Tradier
- Robinhood
- Other/manual brokerages

Only Alpaca currently has live order execution code.

## MetaMask

The dashboard includes a MetaMask connection button.

It stores only:

- public wallet address
- provider name
- browser user agent
- timestamp

No private keys or seed phrases are collected.

## Vercel Deployment

Deployment files:

- `vercel.json`
- `api/index.py`
- `.env.vercel.example`
- `VERCEL_DEPLOYMENT.md`

Vercel can host the web dashboard online. It should not run the always-on bot
loop because serverless functions are not persistent workers.

## Validation Completed

Latest checks completed:

- Python compile check passed
- Flask app import passed
- Signal engine smoke test passed
- Dashboard route rendered successfully
- Authenticated API smoke tests passed:
  - `/api/chart-data`
  - `/api/intelligence`
  - `/api/watchlist`
  - `/api/macro`
  - `/api/insiders`
  - `/api/performance`

## Current Limitations

- No real win ratio is proven yet.
- Backtesting is not implemented yet.
- Closed-trade PnL outcome reconciliation is not implemented yet.
- TradingView webhook ingestion is not implemented yet.
- Broker execution is Alpaca-only.
- Vercel can host the dashboard, but not the bot loop.
- AI outputs are not guaranteed predictions and must remain advisory.

## Recommended Next Steps

1. Run dry-run sessions across several full market days.
2. Add closed-trade outcome reconciliation for true win rate and PnL.
3. Add historical backtesting for strategies and AlphaCore scoring.
4. Add TradingView webhook ingestion for external alerts.
5. Add a persistent worker/VPS deployment plan for the bot loop.
6. Add broker-specific execution integrations beyond Alpaca only after dry-run
   metrics are reliable.
7. Commit the clean project tree after reviewing uncommitted changes.

## Trading Safety Note

NexoSignal can help analyze charts, score signals, track trades, and organize
research. It does not guarantee profitable trades. Keep paper trading enabled
until the dry-run telemetry, closed-trade outcomes, and backtests show reliable
performance across different market regimes.
