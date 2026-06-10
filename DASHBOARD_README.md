# NexoSignal Dashboard

NexoSignal is a professional-grade autonomous trading dashboard. It supports:

- Login-protected browser dashboard with collapsible sidebar navigation
- Account, cash, buying power, positions, and open orders (Portfolio page)
- TradingView-quality interactive charts — candlestick + volume + SMA/EMA/RSI, auto-refresh every 60 s
- AlphaCore signal feed — confidence scoring, confluence ranking, approval/rejection log
- Multi-agent AI research panel (Gemini + Grok + Claude + Local LLM master)
- Manual buy/sell order form with dry-run capture (Trade page)
- Automated bot start/stop controls with paper/live mode toggle
- Persistent Supabase/Postgres audit log of trade attempts
- Real-time order fill toasts via Socket.IO

## 1. Create Alpaca API Keys

1. Go to https://app.alpaca.markets/
2. Create or log in to your Alpaca account.
3. Start with paper trading.
4. Open API Keys.
5. Copy the paper `ALPACA_API_KEY` and `ALPACA_SECRET_KEY`.

## 2. Fill `.env`

Edit `.env` in this folder:

```env
ALPACA_API_KEY=your_key
ALPACA_SECRET_KEY=your_secret
TRADING_MODE=paper

DASHBOARD_USERNAME=admin
DASHBOARD_PASSWORD=your-local-password
FLASK_SECRET_KEY=replace-with-a-long-random-string
```

Keep `TRADING_MODE=paper` until you have tested the bot. The app does not expose Alpaca fund-transfer endpoints.

## 3. Install Dependencies

```powershell
python -m pip install -r requirements.txt
```

## 4. Run Dashboard

```powershell
python dashboard.py
```

Open:

```text
http://127.0.0.1:5000
```

## 5. Trade Capture

Trade events are stored in Supabase/Postgres. Set this before running locally
or deploying:

```env
SUPABASE_DB_URL=postgresql://postgres:PASSWORD@HOST:5432/postgres?sslmode=require
SUPABASE_PROJECT_URL=https://your-project.supabase.co
SUPABASE_ANON_KEY=your-anon-key
```

The dashboard records:

- Manual dry-runs
- Manual submitted orders
- Manual failures
- Bot dry-runs
- Bot submitted orders
- Bot rejections/errors

## 6. Signal Intelligence

The bot now scores every strategy signal before it can become a trade.

Stored fields include:

- base signal from the selected strategy
- final signal after confidence filtering
- confidence score
- approval status
- indicator snapshot
- reason text

Configure the approval threshold:

```env
SIGNAL_MIN_CONFIDENCE=70
```

Generate a dry-run report:

```powershell
python scripts/dry_run_report.py
```

## 7. Supabase PostgreSQL

1. Go to https://supabase.com/dashboard/projects
2. Create a project.
3. Open Project Settings -> Database.
4. Copy the direct Postgres connection string.
5. Put it in `.env`:

```env
SUPABASE_DB_URL=postgresql://postgres:PASSWORD@HOST:5432/postgres?sslmode=require
SUPABASE_PROJECT_URL=https://your-project.supabase.co
SUPABASE_ANON_KEY=your-anon-key
```

The app creates the tables on startup. You can also run `supabase_schema.sql`
in the Supabase SQL editor.

## 8. MetaMask

The dashboard has a Connect MetaMask button. It stores only the public wallet
address in `wallet_connections`; it never receives or stores a private key.

To use it:

1. Install MetaMask in the browser.
2. Log into the dashboard.
3. Click Connect MetaMask.
4. Approve the connection prompt.

## 9. Vercel

Vercel can host the dashboard via `api/index.py` and `vercel.json`.

Set these environment variables in Vercel:

```env
ALPACA_API_KEY=...
ALPACA_SECRET_KEY=...
TRADING_MODE=paper
DASHBOARD_USERNAME=...
DASHBOARD_PASSWORD=...
FLASK_SECRET_KEY=...
SUPABASE_DB_URL=...
SUPABASE_PROJECT_URL=...
SUPABASE_ANON_KEY=...
```

Do not run the bot loop on Vercel. Serverless functions are not built for
continuous background trading loops. Use Vercel for the dashboard and run the
bot locally or on a VPS.

---

## 10. Paper Mode Testing Checklist

Use this checklist before enabling live trading. All items should pass in paper mode first.

### Environment safety

- [ ] `TRADING_MODE=paper` in `.env`
- [ ] `ALPACA_PAPER=true` in `.env`
- [ ] `LIVE_TRADING=false` in `.env`
- [ ] `AUTONOMOUS_TRADING=false` in `.env`
- [ ] `REQUIRE_MANUAL_APPROVAL=true` in `.env`
- [ ] `FLASK_SECRET_KEY` is a long random string (not the default)
- [ ] `DASHBOARD_PASSWORD` is changed from the default
- [ ] `.env` is in `.gitignore` and not committed

### Dashboard startup

- [ ] `python dashboard.py` starts without errors
- [ ] Startup config banner shows ✓ for Alpaca and Supabase
- [ ] `/health` returns `{ "ok": true, "mode": "paper" }`
- [ ] `/login` shows the login form
- [ ] Login with configured credentials works; invalid password is rejected

### Portfolio page (`/`)

- [ ] Account equity, cash, and buying power display
- [ ] Positions table loads (empty is fine on a fresh paper account)
- [ ] Open orders table loads
- [ ] AlphaCore Top 3 predictions load
- [ ] No "Broker unavailable" error when Alpaca keys are valid

### Chart page (`/chart/AAPL`)

- [ ] Candlestick chart renders with OHLCV bars
- [ ] Volume histogram appears at the bottom of the chart
- [ ] SMA 20 and SMA 50 overlay lines appear (active by default)
- [ ] RSI 14 sub-chart syncs when scrolling the main chart
- [ ] Timeframe buttons (5m / 15m / 1H / 1D / 1W) switch data
- [ ] Indicator toggles (SMA20 / SMA50 / EMA9 / RSI / Vol) show/hide correctly
- [ ] AlphaCore signal box shows BUY / SELL / HOLD
- [ ] Side panel OHLCV stats update from last bar
- [ ] Pressing `/` focuses the symbol search input
- [ ] Typing a new symbol and pressing Enter navigates to that chart
- [ ] Auto-refresh dot is visible (updates every 60 s)
- [ ] `/api/chart/AAPL/ohlcv?timeframe=1Day&limit=5` returns valid JSON
- [ ] `/api/chart/AAPL/ohlcv?timeframe=bad` returns HTTP 400 with error message

### Dry-run order flow

- [ ] Navigate to Trade page
- [ ] Enter symbol, side=buy, qty=1, check "Dry run"
- [ ] Submit — should flash "Dry run recorded"
- [ ] `/api/nexosignal/status` still shows `paper_mode: true`
- [ ] Trade Ledger shows a `dry_run` entry with `status: dry_run`
- [ ] `signal_events` table in Supabase has the corresponding signal

### Bot start/stop

- [ ] Start bot with Dry run checked → "Bot started" flash
- [ ] `/api/agent-status` returns `{ "running": true }`
- [ ] Stop bot → "Bot stop requested" flash
- [ ] `/api/agent-status` returns `{ "running": false }`

### Safety guardrails

- [ ] Submitting an order with `LIVE_TRADING=false` does not create a live order
- [ ] Mode toggle in topbar switches to "live" only when `LIVE_TRADING=true`
- [ ] Circuit breaker flag is visible when Guard raises a strike
- [ ] Daily loss limit blocks new orders when `daily_pnl` exceeds `DAILY_LOSS_LIMIT_PCT`

### Real-time (Socket.IO)

- [ ] Browser console shows no WebSocket errors on load
- [ ] Submitting a manual order (not dry-run) triggers an `order_fill` toast when the order fills

### AI research (optional — only if keys set)

- [ ] `/research` page loads without 500 errors
- [ ] Running AI research for AAPL returns a consensus signal
- [ ] Research panel shows advisory warning ("AI layer is advisory")

### Before enabling live trading

- [ ] Reviewed at least 10 dry-run signals and confirmed they make sense
- [ ] Confirmed stop-loss and take-profit levels in the risk/reward panel
- [ ] Set conservative limits: `MAX_ORDER_VALUE_USD`, `MAX_POSITION_SIZE_PCT`, `DAILY_LOSS_LIMIT_PCT`
- [ ] Set `REQUIRE_MANUAL_APPROVAL=true` to require human confirmation for each trade
- [ ] Confirmed Alpaca paper account balance is what you expect
- [ ] Only then: change `TRADING_MODE=live`, `LIVE_TRADING=true`, `ALPACA_PAPER=false`

**NexoSignal does not guarantee profitable trades. All trading involves risk of loss.**
