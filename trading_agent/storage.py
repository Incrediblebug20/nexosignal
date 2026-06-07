import json
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from . import config


@contextmanager
def postgres_connect() -> Iterator[Any]:
    if not config.SUPABASE_DB_URL:
        raise RuntimeError("SUPABASE_DB_URL is required. This project stores trades in Supabase/Postgres only.")

    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(config.SUPABASE_DB_URL, row_factory=dict_row) as conn:
        yield conn


def init_db() -> None:
    with postgres_connect() as conn:
        conn.execute(
            """
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
            )
            """
        )
        conn.execute("alter table trade_events add column if not exists target_stop_loss double precision")
        conn.execute("alter table trade_events add column if not exists target_take_profit double precision")
        conn.execute("alter table trade_events add column if not exists current_risk_reward_ratio double precision")
        conn.execute("alter table trade_events add column if not exists execution_mode text not null default 'manual'")
        conn.execute(
            """
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
            """
        )
        conn.execute(
            """
            create table if not exists wallet_connections (
                id bigserial primary key,
                created_at timestamptz not null,
                wallet_address text not null,
                provider text not null default 'metamask',
                user_agent text
            )
            """
        )
        conn.execute(
            """
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
            )
            """
        )
        conn.execute(
            """
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
            )
            """
        )
        conn.execute("alter table signal_events add column if not exists confluence_score double precision")
        conn.execute("alter table signal_events add column if not exists order_book_imbalance double precision")
        conn.execute(
            """
            create table if not exists agent_events (
                id bigserial primary key,
                created_at timestamptz not null,
                layer text not null,
                event_type text not null,
                message text not null,
                severity text not null default 'info',
                symbol text,
                payload_json jsonb
            )
            """
        )
        conn.execute("create index if not exists trade_events_created_at_idx on trade_events (created_at desc)")
        conn.execute("create index if not exists wallet_connections_created_at_idx on wallet_connections (created_at desc)")
        conn.execute("create index if not exists brokerage_connections_created_at_idx on brokerage_connections (created_at desc)")
        conn.execute("create index if not exists signal_events_created_at_idx on signal_events (created_at desc)")
        conn.execute("create index if not exists agent_events_created_at_idx on agent_events (created_at desc)")
        conn.execute("alter table trade_events enable row level security")
        conn.execute("alter table wallet_connections enable row level security")
        conn.execute("alter table brokerage_connections enable row level security")
        conn.execute("alter table signal_events enable row level security")
        conn.execute("alter table agent_events enable row level security")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def record_trade_event(
    *,
    source: str,
    symbol: str,
    side: str,
    qty: float,
    order_type: str,
    status: str,
    order_id: str | None = None,
    price: float | None = None,
    strategy: str | None = None,
    dry_run: bool = False,
    error: str | None = None,
    raw: dict[str, Any] | None = None,
    target_stop_loss: float | None = None,
    target_take_profit: float | None = None,
    current_risk_reward_ratio: float | None = None,
    execution_mode: str = "manual",
) -> None:
    init_db()
    raw_payload = json.dumps(raw, default=str) if raw is not None else None

    with postgres_connect() as conn:
        conn.execute(
            """
            insert into trade_events
                (created_at, source, symbol, side, qty, order_type, status,
                 order_id, price, strategy, dry_run, error, raw_json,
                 target_stop_loss, target_take_profit, current_risk_reward_ratio, execution_mode)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb,
                    %s, %s, %s, %s)
            """,
            (
                _utc_now(),
                source,
                symbol.upper(),
                side.lower(),
                float(qty),
                order_type,
                status,
                order_id,
                price,
                strategy,
                dry_run,
                error,
                raw_payload,
                target_stop_loss,
                target_take_profit,
                current_risk_reward_ratio,
                execution_mode,
            ),
        )


def list_trade_events(limit: int = 100) -> list[dict[str, Any]]:
    init_db()
    with postgres_connect() as conn:
        rows = conn.execute(
            """
            select *
            from trade_events
            order by id desc
            limit %s
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def trade_summary() -> dict[str, Any]:
    init_db()
    with postgres_connect() as conn:
        row = conn.execute(
            """
            select
                count(*) as total,
                coalesce(sum(case when status in ('accepted', 'filled', 'submitted', 'new') then 1 else 0 end), 0) as accepted,
                coalesce(sum(case when status in ('rejected', 'failed', 'error') then 1 else 0 end), 0) as failed,
                coalesce(sum(case when dry_run then 1 else 0 end), 0) as dry_runs
            from trade_events
            """
        ).fetchone()
    return dict(row)


def record_wallet_connection(wallet_address: str, user_agent: str | None = None) -> None:
    init_db()
    with postgres_connect() as conn:
        conn.execute(
            """
            insert into wallet_connections (created_at, wallet_address, provider, user_agent)
            values (%s, %s, 'metamask', %s)
            """,
            (_utc_now(), wallet_address.lower(), user_agent),
        )


def latest_wallet_connection() -> dict[str, Any] | None:
    init_db()
    with postgres_connect() as conn:
        row = conn.execute(
            """
            select *
            from wallet_connections
            order by id desc
            limit 1
            """
        ).fetchone()
    return dict(row) if row else None


def record_brokerage_connection(
    *,
    provider: str,
    account_label: str,
    environment: str,
    status: str,
    account_id: str | None = None,
    api_key_last4: str | None = None,
    notes: str | None = None,
) -> None:
    init_db()
    created_at = _utc_now()

    with postgres_connect() as conn:
        if account_id:
            existing = conn.execute(
                """
                select id from brokerage_connections
                where provider = %s and account_id = %s
                order by id desc
                limit 1
                """,
                (provider, account_id),
            ).fetchone()
            if existing:
                conn.execute(
                    """
                    update brokerage_connections
                    set created_at = %s,
                        account_label = %s,
                        environment = %s,
                        status = %s,
                        api_key_last4 = %s,
                        notes = %s
                    where id = %s
                    """,
                    (
                        created_at,
                        account_label,
                        environment,
                        status,
                        api_key_last4,
                        notes,
                        existing["id"],
                    ),
                )
                return
        conn.execute(
            """
            insert into brokerage_connections
                (created_at, provider, account_label, account_id, environment, status, api_key_last4, notes)
            values (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (created_at, provider, account_label, account_id, environment, status, api_key_last4, notes),
        )


def list_brokerage_connections(limit: int = 50) -> list[dict[str, Any]]:
    init_db()
    with postgres_connect() as conn:
        rows = conn.execute(
            """
            select *
            from brokerage_connections
            order by id desc
            limit %s
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def record_signal_event(
    *,
    symbol: str,
    strategy: str,
    base_signal: str,
    final_signal: str,
    confidence: float,
    approved: bool,
    reason: str,
    price: float,
    indicators: dict[str, Any],
    confluence_score: float | None = None,
    order_book_imbalance: float | None = None,
) -> None:
    init_db()
    indicators_payload = json.dumps(indicators, default=str)

    with postgres_connect() as conn:
        conn.execute(
            """
            insert into signal_events
                (created_at, symbol, strategy, base_signal, final_signal,
                 confidence, approved, reason, price, indicators_json,
                 confluence_score, order_book_imbalance)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
            """,
            (
                _utc_now(),
                symbol.upper(),
                strategy,
                base_signal,
                final_signal,
                confidence,
                approved,
                reason,
                price,
                indicators_payload,
                confluence_score,
                order_book_imbalance,
            ),
        )


def list_signal_events(limit: int = 100) -> list[dict[str, Any]]:
    init_db()
    with postgres_connect() as conn:
        rows = conn.execute(
            """
            select *
            from signal_events
            order by id desc
            limit %s
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def signal_summary() -> dict[str, Any]:
    init_db()
    with postgres_connect() as conn:
        row = conn.execute(
            """
            select
                count(*) as total,
                coalesce(sum(case when approved then 1 else 0 end), 0) as approved,
                avg(confidence) as avg_confidence
            from signal_events
            """
        ).fetchone()
    return dict(row)


def record_agent_event(
    *,
    layer: str,
    event_type: str,
    message: str,
    severity: str = "info",
    symbol: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    init_db()
    payload_json = json.dumps(payload, default=str) if payload is not None else None
    with postgres_connect() as conn:
        conn.execute(
            """
            insert into agent_events
                (created_at, layer, event_type, message, severity, symbol, payload_json)
            values (%s, %s, %s, %s, %s, %s, %s::jsonb)
            """,
            (_utc_now(), layer, event_type, message, severity, symbol, payload_json),
        )


def list_agent_events(limit: int = 100) -> list[dict[str, Any]]:
    init_db()
    with postgres_connect() as conn:
        rows = conn.execute(
            """
            select *
            from agent_events
            order by id desc
            limit %s
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]
