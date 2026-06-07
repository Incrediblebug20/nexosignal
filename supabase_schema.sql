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
