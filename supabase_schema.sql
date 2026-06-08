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
