# Deploy NexoSignal Online

This deploys the dashboard so you can log in from any device.

Important: Vercel is serverless. Use it for the dashboard, trade history, wallet
connections, and brokerage connection registry. Do not run the always-on bot loop
from Vercel; run the bot locally or on a VPS.

## 1. Create Supabase Database

1. Go to https://supabase.com/dashboard/projects
2. Create a project.
3. Open SQL Editor.
4. Run `supabase_schema.sql`.
5. Copy the direct Postgres connection string.

Use this env on Vercel:

```env
SUPABASE_DB_URL=
SUPABASE_PROJECT_URL=
SUPABASE_ANON_KEY=
```

## 2. Create Vercel Project

1. Go to https://vercel.com/new
2. Import this project folder/repo.
3. Framework preset: Other.
4. Add all variables from `.env.vercel.example`.
5. Deploy.

## 3. Required Vercel Env Vars

```env
DASHBOARD_BRAND_NAME=NexoSignal
DASHBOARD_USERNAME=admin
DASHBOARD_PASSWORD=replace-with-a-strong-password
FLASK_SECRET_KEY=replace-with-a-long-random-secret
ALPACA_API_KEY=your_alpaca_paper_key
ALPACA_SECRET_KEY=your_alpaca_paper_secret
TRADING_MODE=paper
SUPABASE_DB_URL=your_supabase_postgres_url
SIGNAL_MIN_CONFIDENCE=70
```

## 4. Login From Any Device

After deploy, Vercel gives you a URL like:

```text
https://your-project.vercel.app
```

Use `DASHBOARD_USERNAME` and `DASHBOARD_PASSWORD` to log in.

## 5. Brokerage Accounts

The dashboard can store brokerage connection records for:

- Alpaca
- Interactive Brokers
- Charles Schwab
- Tradier
- Robinhood
- Other

Only Alpaca is currently wired for live API trading in this codebase. Other
brokerages are tracked as connection records until their individual OAuth/API
connectors are added.
