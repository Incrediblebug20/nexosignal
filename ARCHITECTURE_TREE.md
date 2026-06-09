# Clean Architecture Tree

```text
AI Agent ploymarket/
|-- api/
|   `-- index.py                    # Vercel serverless Flask entrypoint
|-- scripts/
|   `-- dry_run_report.py            # Signal/trade dry-run reporting
|-- static/
|   |-- dashboard.js                 # Charts, live metrics, AI research UI
|   |-- metamask.js                  # MetaMask public wallet connection
|   `-- styles.css                   # Dashboard styling
|-- templates/
|   |-- base.html                    # Shared dashboard shell
|   |-- dashboard.html               # Main operations dashboard
|   `-- login.html                   # Login screen
|-- trading_agent/
|   |-- __init__.py
|   |-- agent.py                     # Bot loop and signal execution flow
|   |-- ai_research.py               # Gemini/Grok/Claude research scaffold
|   |-- broker.py                    # Alpaca API wrapper
|   |-- config.py                    # Environment/config loading
|   |-- restrictions.py              # Safety guardrails
|   |-- signal_engine.py             # Indicator scoring and confidence gates
|   |-- storage.py                   # Supabase/Postgres persistence layer
|   `-- strategy.py                  # SMA, RSI, VWAP strategies
|-- polymarket-arb-bot/              # Local reference repo, ignored by this Git repo
|-- .env                             # Local secrets/config, ignored by git
|-- .env.example                     # Local env template
|-- .env.vercel.example              # Vercel env template
|-- .gitignore
|-- dashboard.py                     # Main Flask app
|-- main.py                          # Original CLI trading agent
|-- requirements.txt
|-- supabase_schema.sql              # Supabase/Postgres schema
|-- vercel.json                      # Vercel routing/build config
|-- DASHBOARD_README.md
|-- ARCHITECTURE_TREE.md
|-- PROJECT_STRUCTURE_ANALYSIS.md   # Layers, modules, data flow, entry points
|-- PROJECT_SUMMARY_REPORT.md
`-- VERCEL_DEPLOYMENT.md
```

## Removed Generated And Extra Files

- Root `__pycache__/`
- `api/__pycache__/`
- `scripts/__pycache__/`
- `trading_agent/__pycache__/`
- `data/trades.sqlite3`
- `data/`
- `_outside_architecture/`
- `polymarket-arb-bot/node_modules/`

`polymarket-arb-bot/` was kept because it is a separate source repository, not generated clutter. Its dependencies can be restored later with `npm install` inside that folder.
It is ignored by this Git repo so the dashboard project can be pushed without embedding another repository.
