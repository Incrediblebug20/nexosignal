create table if not exists trade_events (
    id bigserial primary key,
    created_at timestamptz not null,
    source text not null,
    symbol text not null,
    side text not null,
    qty double precision not null,
    order_type text not null,
    status text not null,
    order_id text,
    price double precision,
    strategy text,
    dry_run boolean not null default false,
    error text,
    raw_json jsonb
);

create index if not exists trade_events_created_at_idx
    on trade_events (created_at desc);

alter table trade_events enable row level security;

alter table trade_events add column if not exists target_stop_loss double precision;
alter table trade_events add column if not exists target_take_profit double precision;
alter table trade_events add column if not exists current_risk_reward_ratio double precision;
alter table trade_events add column if not exists execution_mode text not null default 'manual';
alter table trade_events add column if not exists asset_type text;
alter table trade_events add column if not exists order_lifecycle_status text;
alter table trade_events add column if not exists entry_price double precision;
alter table trade_events add column if not exists exit_price double precision;
alter table trade_events add column if not exists realized_risk_reward_ratio double precision;
alter table trade_events add column if not exists realized_pnl double precision;
alter table trade_events add column if not exists unrealized_pnl double precision;
alter table trade_events add column if not exists position_size double precision;
alter table trade_events add column if not exists dollar_risk double precision;
alter table trade_events add column if not exists notional_exposure double precision;
alter table trade_events add column if not exists strategy_name text;
alter table trade_events add column if not exists circuit_breaker_triggered boolean not null default false;
alter table trade_events add column if not exists risk_check_passed boolean;
alter table trade_events add column if not exists risk_rejection_reason text;

do $$
begin
    if not exists (
        select 1
        from pg_constraint
        where conname = 'trade_events_execution_mode_chk'
    ) then
        alter table trade_events
        add constraint trade_events_execution_mode_chk
        check (execution_mode in ('manual', 'autonomous_agent'));
    end if;
end $$;

create table if not exists wallet_connections (
    id bigserial primary key,
    created_at timestamptz not null,
    wallet_address text not null,
    provider text not null default 'metamask',
    user_agent text
);

create index if not exists wallet_connections_created_at_idx
    on wallet_connections (created_at desc);

alter table wallet_connections enable row level security;

create table if not exists brokerage_connections (
    id bigserial primary key,
    created_at timestamptz not null,
    provider text not null,
    account_label text not null,
    account_id text,
    environment text not null,
    status text not null,
    api_key_last4 text,
    notes text
);

create index if not exists brokerage_connections_created_at_idx
    on brokerage_connections (created_at desc);

alter table brokerage_connections enable row level security;

create table if not exists signal_events (
    id bigserial primary key,
    created_at timestamptz not null,
    symbol text not null,
    strategy text not null,
    base_signal text not null,
    final_signal text not null,
    confidence double precision not null,
    approved boolean not null,
    reason text not null,
    price double precision not null,
    indicators_json jsonb not null,
    outcome_status text not null default 'open',
    outcome_price double precision,
    outcome_pnl double precision
);

create index if not exists signal_events_created_at_idx
    on signal_events (created_at desc);

alter table signal_events enable row level security;

alter table signal_events add column if not exists confluence_score double precision;
alter table signal_events add column if not exists order_book_imbalance double precision;
alter table signal_events add column if not exists asset_type text;
alter table signal_events add column if not exists strategy_name text;
alter table signal_events add column if not exists confidence_score double precision;
alter table signal_events add column if not exists expected_direction text;
alter table signal_events add column if not exists entry_price double precision;
alter table signal_events add column if not exists target_stop_loss double precision;
alter table signal_events add column if not exists target_take_profit double precision;
alter table signal_events add column if not exists risk_reward_ratio double precision;
alter table signal_events add column if not exists expected_r_multiple double precision;
alter table signal_events add column if not exists expected_value_score double precision;
alter table signal_events add column if not exists prediction_rank integer;
alter table signal_events add column if not exists signal_reason text;
alter table signal_events add column if not exists blocked_reason text;
alter table signal_events add column if not exists stale_data boolean not null default false;

create table if not exists agent_events (
    id bigserial primary key,
    created_at timestamptz not null,
    layer text not null,
    event_type text not null,
    message text not null,
    severity text not null default 'info',
    symbol text,
    payload_json jsonb
);

create index if not exists agent_events_created_at_idx
    on agent_events (created_at desc);

alter table agent_events enable row level security;

-- ── NexoSignal Phase 2 Intelligence Extension ─────────────────────────────

-- NexoSignal Scout: dynamic watchlist, rebuilt every Saturday 8 AM
create table if not exists watchlist (
    id bigserial primary key,
    symbol text not null unique,
    score_growth double precision,
    score_value double precision,
    score_yield double precision,
    score_sentiment double precision,
    score_insider double precision,
    score_earnings_quality double precision,
    composite_score double precision,
    category text,
    rank_position integer,
    last_rebuild_at timestamptz
);

create index if not exists watchlist_composite_idx on watchlist (composite_score desc);
alter table watchlist enable row level security;

-- NexoSignal Lens: macro regime snapshots, refreshed every Sunday 8 AM
create table if not exists macro_snapshots (
    id bigserial primary key,
    captured_at timestamptz not null,
    dgs10 double precision,
    dgs2 double precision,
    spread double precision,
    jobless_claims double precision,
    regime text not null
);

create index if not exists macro_snapshots_captured_at_idx on macro_snapshots (captured_at desc);
alter table macro_snapshots enable row level security;

-- NexoSignal Lens: SEC EDGAR Form 4 insider filings, parsed every weekday 6 PM
create table if not exists insider_activity (
    id bigserial primary key,
    filed_at timestamptz not null,
    symbol text not null,
    insider_name text,
    title text,
    transaction_type text,
    shares double precision,
    value double precision
);

create index if not exists insider_activity_symbol_idx on insider_activity (symbol, filed_at desc);
alter table insider_activity enable row level security;

-- NexoSignal Lens: earnings quality analysis results
create table if not exists earnings_analysis (
    id bigserial primary key,
    analyzed_at timestamptz not null,
    symbol text not null,
    period text,
    eps_actual double precision,
    eps_estimate double precision,
    beat_pct double precision,
    lens_summary text,
    lens_quality_score double precision
);

create index if not exists earnings_analysis_symbol_idx on earnings_analysis (symbol, analyzed_at desc);
alter table earnings_analysis enable row level security;

-- NexoSignal Lens: annual report (10-K) analysis results
create table if not exists annual_report_analysis (
    id bigserial primary key,
    analyzed_at timestamptz not null,
    symbol text not null,
    fiscal_year text,
    revenue_yoy double precision,
    moat_score double precision,
    red_flag_count integer,
    lens_summary text
);

create index if not exists annual_report_symbol_idx on annual_report_analysis (symbol, analyzed_at desc);
alter table annual_report_analysis enable row level security;

-- NexoSignal Guard: per-position and portfolio VaR risk metrics
create table if not exists risk_metrics (
    id bigserial primary key,
    checked_at timestamptz not null,
    symbol text not null,
    var_1d double precision,
    var_1d_pct double precision,
    portfolio_var double precision,
    correlation_flag boolean not null default false
);

create index if not exists risk_metrics_symbol_idx on risk_metrics (symbol, checked_at desc);
alter table risk_metrics enable row level security;

-- NexoSignal Telemetry: latency, slippage, PnL, win-rate, and uptime snapshots
create table if not exists performance_events (
    id bigserial primary key,
    created_at timestamptz not null,
    stage text not null,
    symbol text,
    latency_ms double precision,
    slippage_bps double precision,
    mark_to_market_pnl double precision,
    realized_pnl double precision,
    win_rate double precision,
    trade_count integer,
    uptime_seconds integer,
    payload_json jsonb
);

create index if not exists performance_events_created_at_idx on performance_events (created_at desc);
alter table performance_events enable row level security;

create table if not exists daily_summaries (
    id bigserial primary key,
    trade_date date not null unique,
    total_trades integer not null default 0,
    wins integer not null default 0,
    losses integer not null default 0,
    win_loss_ratio double precision,
    net_pnl double precision,
    average_r double precision,
    max_drawdown double precision,
    circuit_breaker_triggered boolean not null default false,
    top_prediction_symbol text,
    notes text,
    created_at timestamptz not null default now()
);

create index if not exists daily_summaries_trade_date_idx on daily_summaries (trade_date desc);
alter table daily_summaries enable row level security;

-- ── NexoSignal Autopilot: Strategy Portfolios ─────────────────────────────
-- Each row is one named strategy configuration that can be armed as an autopilot.
create table if not exists strategy_portfolios (
    id bigserial primary key,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    name text not null,
    description text,
    symbols text not null default '',          -- comma-separated ticker list
    strategy_type text not null default 'sma_crossover',
    allocation_pct double precision not null default 0.1,  -- fraction of portfolio (0.1 = 10%)
    max_position_usd double precision not null default 1000.0,
    max_drawdown_pct double precision not null default 0.05,
    daily_loss_limit_usd double precision not null default 500.0,
    min_confidence double precision not null default 70.0,
    min_risk_reward double precision not null default 5.0,
    autopilot_active boolean not null default false,
    dry_run boolean not null default true,
    total_trades integer not null default 0,
    wins integer not null default 0,
    losses integer not null default 0,
    total_pnl double precision not null default 0.0
);

create index if not exists strategy_portfolios_created_at_idx
    on strategy_portfolios (created_at desc);

alter table strategy_portfolios enable row level security;

-- NexoSignal Lens: AI intelligence report cache
-- Stores structured summaries and custom query results from the Lens layer.
create table if not exists lens_reports (
    id bigserial primary key,
    created_at timestamptz not null default now(),
    symbol text not null,
    report_type text not null default 'summary',  -- 'summary' | 'ask'
    content_json jsonb not null default '{}',
    conviction_score double precision,
    expires_at timestamptz,
    query text                                     -- populated for report_type='ask'
);

create index if not exists lens_reports_symbol_type_idx
    on lens_reports (symbol, report_type, created_at desc);

alter table lens_reports enable row level security;

-- NexoSignal Backtester: historical strategy simulation run results
create table if not exists backtest_runs (
    id bigserial primary key,
    created_at timestamptz not null default now(),
    run_id text not null unique,
    symbols text not null,
    strategy text not null,
    params_json jsonb not null default '{}',
    date_from text not null,
    date_to text not null,
    initial_capital double precision not null,
    final_equity double precision,
    total_return_pct double precision,
    max_drawdown_pct double precision,
    sharpe_ratio double precision,
    win_rate double precision,
    num_trades integer,
    data_source text
);

create index if not exists backtest_runs_created_at_idx
    on backtest_runs (created_at desc);

alter table backtest_runs enable row level security;
