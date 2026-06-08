import json
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from . import config

_DB_INIT_LOCK = threading.Lock()
_DB_INITIALIZED = False


@contextmanager
def postgres_connect() -> Iterator[Any]:
    if not config.SUPABASE_DB_URL:
        raise RuntimeError("SUPABASE_DB_URL is required. This project stores trades in Supabase/Postgres only.")

    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(config.SUPABASE_DB_URL, row_factory=dict_row) as conn:
        yield conn


def init_db() -> None:
    global _DB_INITIALIZED
    if _DB_INITIALIZED:
        return
    with _DB_INIT_LOCK:
        if _DB_INITIALIZED:
            return
        _init_db_uncached()
        _DB_INITIALIZED = True


def _init_db_uncached() -> None:
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

        # ── Phase 2: Intelligence Extension tables ─────────────────────────
        conn.execute(
            """
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
            )
            """
        )
        conn.execute("create index if not exists watchlist_composite_idx on watchlist (composite_score desc)")
        conn.execute("alter table watchlist enable row level security")

        conn.execute(
            """
            create table if not exists macro_snapshots (
                id bigserial primary key,
                captured_at timestamptz not null,
                dgs10 double precision,
                dgs2 double precision,
                spread double precision,
                jobless_claims double precision,
                regime text not null
            )
            """
        )
        conn.execute("create index if not exists macro_snapshots_captured_at_idx on macro_snapshots (captured_at desc)")
        conn.execute("alter table macro_snapshots enable row level security")

        conn.execute(
            """
            create table if not exists insider_activity (
                id bigserial primary key,
                filed_at timestamptz not null,
                symbol text not null,
                insider_name text,
                title text,
                transaction_type text,
                shares double precision,
                value double precision
            )
            """
        )
        conn.execute("create index if not exists insider_activity_symbol_idx on insider_activity (symbol, filed_at desc)")
        conn.execute("alter table insider_activity enable row level security")

        conn.execute(
            """
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
            )
            """
        )
        conn.execute("create index if not exists earnings_analysis_symbol_idx on earnings_analysis (symbol, analyzed_at desc)")
        conn.execute("alter table earnings_analysis enable row level security")

        conn.execute(
            """
            create table if not exists annual_report_analysis (
                id bigserial primary key,
                analyzed_at timestamptz not null,
                symbol text not null,
                fiscal_year text,
                revenue_yoy double precision,
                moat_score double precision,
                red_flag_count integer,
                lens_summary text
            )
            """
        )
        conn.execute("create index if not exists annual_report_symbol_idx on annual_report_analysis (symbol, analyzed_at desc)")
        conn.execute("alter table annual_report_analysis enable row level security")

        conn.execute(
            """
            create table if not exists risk_metrics (
                id bigserial primary key,
                checked_at timestamptz not null,
                symbol text not null,
                var_1d double precision,
                var_1d_pct double precision,
                portfolio_var double precision,
                correlation_flag boolean not null default false
            )
            """
        )
        conn.execute("create index if not exists risk_metrics_symbol_idx on risk_metrics (symbol, checked_at desc)")
        conn.execute("alter table risk_metrics enable row level security")

        conn.execute(
            """
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
            )
            """
        )
        conn.execute("create index if not exists performance_events_created_at_idx on performance_events (created_at desc)")
        conn.execute("alter table performance_events enable row level security")


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


# ── Phase 2: NexoSignal Scout — Watchlist Ledger ──────────────────────────

def upsert_watchlist_entry(
    *,
    symbol: str,
    composite_score: float,
    category: str,
    rank_position: int,
    score_growth: float = 0.0,
    score_value: float = 0.0,
    score_yield: float = 0.0,
    score_sentiment: float = 0.0,
    score_insider: float = 0.0,
    score_earnings_quality: float = 0.0,
) -> None:
    init_db()
    now = _utc_now()
    with postgres_connect() as conn:
        conn.execute(
            """
            insert into watchlist
                (symbol, composite_score, category, rank_position,
                 score_growth, score_value, score_yield, score_sentiment,
                 score_insider, score_earnings_quality, last_rebuild_at)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            on conflict (symbol) do update set
                composite_score = excluded.composite_score,
                category = excluded.category,
                rank_position = excluded.rank_position,
                score_growth = excluded.score_growth,
                score_value = excluded.score_value,
                score_yield = excluded.score_yield,
                score_sentiment = excluded.score_sentiment,
                score_insider = excluded.score_insider,
                score_earnings_quality = excluded.score_earnings_quality,
                last_rebuild_at = excluded.last_rebuild_at
            """,
            (
                symbol.upper(), composite_score, category, rank_position,
                score_growth, score_value, score_yield, score_sentiment,
                score_insider, score_earnings_quality, now,
            ),
        )


def list_watchlist(limit: int = 50) -> list[dict[str, Any]]:
    init_db()
    with postgres_connect() as conn:
        rows = conn.execute(
            """
            select * from watchlist
            order by rank_position asc nulls last
            limit %s
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


# ── Phase 2: NexoSignal Lens — Macro Snapshots ────────────────────────────

def record_macro_snapshot(
    *,
    dgs10: float | None,
    dgs2: float | None,
    spread: float | None,
    jobless_claims: float | None,
    regime: str,
) -> None:
    init_db()
    with postgres_connect() as conn:
        conn.execute(
            """
            insert into macro_snapshots
                (captured_at, dgs10, dgs2, spread, jobless_claims, regime)
            values (%s, %s, %s, %s, %s, %s)
            """,
            (_utc_now(), dgs10, dgs2, spread, jobless_claims, regime),
        )


def latest_macro_snapshot() -> dict[str, Any] | None:
    init_db()
    with postgres_connect() as conn:
        row = conn.execute(
            "select * from macro_snapshots order by id desc limit 1"
        ).fetchone()
    return dict(row) if row else None


# ── Phase 2: NexoSignal Lens — Insider Activity ───────────────────────────

def record_insider_activity(
    *,
    symbol: str,
    insider_name: str | None,
    title: str | None,
    transaction_type: str | None,
    shares: float | None,
    value: float | None,
    filed_at: str,
) -> None:
    init_db()
    with postgres_connect() as conn:
        conn.execute(
            """
            insert into insider_activity
                (filed_at, symbol, insider_name, title, transaction_type, shares, value)
            values (%s, %s, %s, %s, %s, %s, %s)
            """,
            (filed_at, symbol.upper(), insider_name, title, transaction_type, shares, value),
        )


def list_insider_activity(limit: int = 50, symbol: str | None = None) -> list[dict[str, Any]]:
    init_db()
    with postgres_connect() as conn:
        if symbol:
            rows = conn.execute(
                "select * from insider_activity where symbol = %s order by filed_at desc limit %s",
                (symbol.upper(), limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "select * from insider_activity order by filed_at desc limit %s",
                (limit,),
            ).fetchall()
    return [dict(row) for row in rows]


# ── Phase 2: NexoSignal Lens — Earnings Analysis ──────────────────────────

def record_earnings_analysis(
    *,
    symbol: str,
    period: str | None,
    eps_actual: float | None,
    eps_estimate: float | None,
    beat_pct: float | None,
    lens_summary: str | None,
    lens_quality_score: float | None,
) -> None:
    init_db()
    with postgres_connect() as conn:
        conn.execute(
            """
            insert into earnings_analysis
                (analyzed_at, symbol, period, eps_actual, eps_estimate,
                 beat_pct, lens_summary, lens_quality_score)
            values (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                _utc_now(), symbol.upper(), period, eps_actual,
                eps_estimate, beat_pct, lens_summary, lens_quality_score,
            ),
        )


def list_earnings_analysis(limit: int = 50, symbol: str | None = None) -> list[dict[str, Any]]:
    init_db()
    with postgres_connect() as conn:
        if symbol:
            rows = conn.execute(
                "select * from earnings_analysis where symbol = %s order by analyzed_at desc limit %s",
                (symbol.upper(), limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "select * from earnings_analysis order by analyzed_at desc limit %s",
                (limit,),
            ).fetchall()
    return [dict(row) for row in rows]


# ── Phase 2: NexoSignal Lens — Annual Report Analysis ────────────────────

def record_annual_report_analysis(
    *,
    symbol: str,
    fiscal_year: str | None,
    revenue_yoy: float | None,
    moat_score: float | None,
    red_flag_count: int | None,
    lens_summary: str | None,
) -> None:
    init_db()
    with postgres_connect() as conn:
        conn.execute(
            """
            insert into annual_report_analysis
                (analyzed_at, symbol, fiscal_year, revenue_yoy,
                 moat_score, red_flag_count, lens_summary)
            values (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                _utc_now(), symbol.upper(), fiscal_year, revenue_yoy,
                moat_score, red_flag_count, lens_summary,
            ),
        )


# ── Phase 2: NexoSignal Guard — Risk Metrics ─────────────────────────────

def record_risk_metrics(
    *,
    symbol: str,
    var_1d: float | None,
    var_1d_pct: float | None,
    portfolio_var: float | None,
    correlation_flag: bool = False,
) -> None:
    init_db()
    with postgres_connect() as conn:
        conn.execute(
            """
            insert into risk_metrics
                (checked_at, symbol, var_1d, var_1d_pct, portfolio_var, correlation_flag)
            values (%s, %s, %s, %s, %s, %s)
            """,
            (_utc_now(), symbol.upper(), var_1d, var_1d_pct, portfolio_var, correlation_flag),
        )


def list_risk_metrics(limit: int = 50) -> list[dict[str, Any]]:
    init_db()
    with postgres_connect() as conn:
        rows = conn.execute(
            "select * from risk_metrics order by checked_at desc limit %s",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


# Phase 3: NexoSignal Telemetry

def record_performance_event(
    *,
    stage: str,
    symbol: str | None = None,
    latency_ms: float | None = None,
    slippage_bps: float | None = None,
    mark_to_market_pnl: float | None = None,
    realized_pnl: float | None = None,
    win_rate: float | None = None,
    trade_count: int | None = None,
    uptime_seconds: int | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    init_db()
    payload_json = json.dumps(payload, default=str) if payload is not None else None
    with postgres_connect() as conn:
        conn.execute(
            """
            insert into performance_events
                (created_at, stage, symbol, latency_ms, slippage_bps,
                 mark_to_market_pnl, realized_pnl, win_rate, trade_count,
                 uptime_seconds, payload_json)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            """,
            (
                _utc_now(), stage, symbol.upper() if symbol else None,
                latency_ms, slippage_bps, mark_to_market_pnl, realized_pnl,
                win_rate, trade_count, uptime_seconds, payload_json,
            ),
        )


def list_performance_events(limit: int = 100) -> list[dict[str, Any]]:
    init_db()
    with postgres_connect() as conn:
        rows = conn.execute(
            """
            select *
            from performance_events
            order by id desc
            limit %s
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]
