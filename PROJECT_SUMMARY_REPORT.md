# Rites Trading Dashboard - Project Summary

## Current State

The project is a local and deployable trading dashboard built around Alpaca,
Supabase/Postgres, MetaMask wallet capture, deterministic signal intelligence,
and an AI research scaffold.

The dashboard is branded as:

```text
Rites Trading Dashboard
```

## Core Components

### Dashboard

- Flask web app in `dashboard.py`
- Login-protected UI
- Branded dashboard header and operations console layout
- Refined operations-console UI with quick navigation and status chips
- Manual order form
- Bot start/stop controls
- Account summary, positions, open orders, trade events, signal events, wallet, and brokerage connections
- Chart/live metric API endpoints for dashboard analytics

### Broker Integration

Currently wired for Alpaca:

- Account lookup
- Clock/market status
- Positions
- Open orders
- Market orders
- Order cancellation
- Market data bars/quotes

Other brokerages can be tracked as connection records:

- Interactive Brokers
- Charles Schwab
- Tradier
- Robinhood
- Other

Only Alpaca is currently implemented for live API trading.

### Safety Controls

The existing broker layer blocks fund-transfer actions and applies:

- Symbol whitelist/blacklist
- Max order value
- Max position size percentage
- Minimum cash reserve
- Max trades per day
- Daily loss limit

The dashboard defaults to paper trading and dry-run operation.

## Signal Intelligence Engine

Implemented in `trading_agent/signal_engine.py`.

Every strategy signal is scored before it can become a trade.

Indicators/features:

- SMA fast
- SMA slow
- RSI
- VWAP
- Volume ratio
- Candle direction
- Candle body strength
- Trend classification

Signal records include:

- Symbol
- Strategy
- Base signal
- Final signal
- Confidence score
- Approval status
- Reason text
- Price
- Indicator snapshot

Approval threshold:

```env
SIGNAL_MIN_CONFIDENCE=70
```

## Strategies

Current strategy modules:

- `sma_crossover`
- `rsi`
- `vwap`

These are technical strategies only. They are not full predictive AI models.

## AI Research Layer

Implemented in `trading_agent/ai_research.py`.

Current design:

- Gemini: market/fundamental + technical research
- Grok/xAI: news/sentiment style research through an OpenAI-compatible API
- Claude/Anthropic: final trade validation with a strict 5:1 reward/risk rule

Current dashboard/API routes:

- `/api/ai-research/<symbol>`
- `/api/price/<symbol>`
- `/api/chart-data`
- `/api/live-metrics`

This layer is advisory. It should not bypass the signal intelligence engine,
broker risk gates, or dry-run validation.

## Storage

Storage is Postgres-only through Supabase:

```env
SUPABASE_DB_URL=...
```

Supabase tables:

- `trade_events`
- `signal_events`
- `wallet_connections`
- `brokerage_connections`

RLS is enabled in `supabase_schema.sql`.

## MetaMask

The dashboard includes a Connect MetaMask button.

It stores only:

- Public wallet address
- Provider name
- User agent
- Timestamp

No private keys are collected or stored.

## Vercel Deployment

Deployment files:

- `vercel.json`
- `api/index.py`
- `.env.vercel.example`
- `VERCEL_DEPLOYMENT.md`

Vercel can host the dashboard online.

Important limitation:

Vercel serverless should not run the always-on trading bot loop. Use it for the
dashboard and database-backed visibility. Run the trading loop locally, on a
VPS, or on a persistent worker host.

## Dry-Run Reporting

Report script:

```powershell
python scripts/dry_run_report.py
```

It summarizes:

- Signal count
- Approved signal count
- Approval rate
- Average confidence
- Captured trade events
- Recent approved signals

## UI Refresh

The dashboard was upgraded into a cleaner operational console:

- Branded header
- Quick navigation
- Overview hero section
- Market/bot/database status chips
- Compact metric tiles
- Guardrails summary
- Improved login screen
- Improved form/table styling
- Mobile responsive layout

## Pending Work

Recommended next items:

1. Deploy dashboard to Vercel after setting production env vars.
2. Run the bot in dry-run mode for several market sessions.
3. Add outcome tracking for signal events after N minutes/hours.
4. Add backtesting against historical Alpaca bars.
5. Add TradingView webhook ingestion.
6. Install/configure AI provider packages and API keys if AI research will be used live.
7. Add broker-specific OAuth/API integrations beyond Alpaca.
8. Add role-based dashboard users if more people will access it.

## AI Provider Plan

Gemini, Grok, DeepSeek, or other models should not directly trigger trades.

Recommended architecture:

```text
Market data + technical indicators + TradingView alerts + AI research
-> signal intelligence engine
-> confidence/risk gate
-> dry-run or order
```

This keeps trading decisions auditable and avoids opaque model-only execution.
