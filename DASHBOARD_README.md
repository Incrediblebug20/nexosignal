# Alpaca Bot Dashboard

Local dashboard for the Alpaca trading bot. It supports:

- Login-protected browser dashboard
- Account, cash, buying power, positions, and open orders
- Manual buy/sell order form with dry-run capture
- Automated bot start/stop controls
- Persistent Supabase/Postgres audit log of trade attempts
- Signal intelligence scoring for dry-run/live decisions

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
