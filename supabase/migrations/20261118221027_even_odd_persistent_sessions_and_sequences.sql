-- Persistent Even/Odd session, sequence and trade-result storage.
-- These tables store audit and paper/demo results; no order placement is exposed.

create table public.even_odd_sessions (
    id uuid primary key default gen_random_uuid(),
    account_mode text not null default 'research'
        check (account_mode in ('research', 'demo', 'real')),
    currency text not null default 'USD'
        check (currency ~ '^[A-Z]{3}$'),
    fixed_stake numeric(20, 8) not null check (fixed_stake > 0),
    max_session_loss numeric(20, 8)
        check (max_session_loss is null or max_session_loss > 0),
    max_trades_per_session smallint not null default 10
        check (max_trades_per_session between 1 and 10),
    max_consecutive_losses smallint not null default 2
        check (max_consecutive_losses between 1 and 2),
    completed_trades smallint not null default 0
        check (completed_trades between 0 and 10),
    wins smallint not null default 0 check (wins between 0 and 10),
    losses smallint not null default 0 check (losses between 0 and 10),
    consecutive_losses smallint not null default 0
        check (consecutive_losses between 0 and 2),
    realized_profit numeric(20, 8) not null default 0,
    status text not null default 'active'
        check (status in ('active', 'closed')),
    started_at timestamptz not null default now(),
    closed_at timestamptz,
    created_at timestamptz not null default now(),
    constraint even_odd_session_outcomes_match
        check (completed_trades = wins + losses),
    constraint even_odd_session_closed_at_check
        check (
            (status = 'active' and closed_at is null)
            or (status = 'closed' and closed_at is not null)
        )
);

create table public.even_odd_sequences (
    id uuid primary key default gen_random_uuid(),
    session_id uuid not null
        references public.even_odd_sessions(id) on delete cascade,
    model_id uuid not null
        references public.even_odd_model_registry(id) on delete restrict,
    model_version text not null,
    planned_trades smallint not null check (planned_trades between 1 and 7),
    completed_trades smallint not null default 0
        check (completed_trades between 0 and 7),
    wins smallint not null default 0 check (wins between 0 and 7),
    losses smallint not null default 0 check (losses between 0 and 7),
    consecutive_losses smallint not null default 0
        check (consecutive_losses between 0 and 2),
    status text not null default 'active'
        check (status in ('active', 'completed', 'stopped')),
    stop_reason text,
    expected_value_snapshot jsonb not null default '{}'::jsonb
        check (jsonb_typeof(expected_value_snapshot) = 'object'),
    started_at timestamptz not null default now(),
    completed_at timestamptz,
    created_at timestamptz not null default now(),
    constraint even_odd_sequence_outcomes_match
        check (completed_trades = wins + losses),
    constraint even_odd_sequence_completion_check
        check (completed_trades <= planned_trades),
    constraint even_odd_sequence_terminal_time_check
        check (
            (status = 'active' and completed_at is null)
            or (status in ('completed', 'stopped') and completed_at is not null)
        )
);

create unique index even_odd_one_active_sequence_per_session_idx
    on public.even_odd_sequences (session_id)
    where status = 'active';
create index even_odd_sequences_model_created_idx
    on public.even_odd_sequences (model_id, created_at desc);
create index even_odd_sequences_session_created_idx
    on public.even_odd_sequences (session_id, created_at desc);

create table public.even_odd_trade_records (
    id uuid primary key default gen_random_uuid(),
    sequence_id uuid not null
        references public.even_odd_sequences(id) on delete cascade,
    trade_number smallint not null check (trade_number between 1 and 7),
    side text not null check (side in ('even', 'odd')),
    stake numeric(20, 8) not null check (stake > 0),
    profit_if_win numeric(20, 8) not null check (profit_if_win > 0),
    loss_if_lose numeric(20, 8) not null check (loss_if_lose > 0),
    even_probability numeric(8, 7) not null
        check (even_probability between 0 and 1),
    lower_even_probability numeric(8, 7) not null
        check (lower_even_probability between 0 and 1),
    upper_even_probability numeric(8, 7) not null
        check (upper_even_probability between 0 and 1),
    conservative_expected_value numeric(20, 8) not null,
    revalidation jsonb not null
        check (jsonb_typeof(revalidation) = 'object'),
    status text not null default 'proposed'
        check (status in ('proposed', 'settled', 'cancelled', 'rejected')),
    outcome text check (outcome in ('win', 'loss')),
    realized_profit numeric(20, 8),
    external_contract_id text,
    created_at timestamptz not null default now(),
    settled_at timestamptz,
    constraint even_odd_trade_probability_interval_check
        check (
            lower_even_probability <= even_probability
            and even_probability <= upper_even_probability
        ),
    constraint even_odd_trade_settlement_check
        check (
            (status = 'settled' and outcome is not null
                and realized_profit is not null and settled_at is not null)
            or
            (status <> 'settled' and outcome is null and settled_at is null)
        ),
    constraint even_odd_trade_number_unique
        unique (sequence_id, trade_number)
);

create unique index even_odd_trade_external_contract_unique_idx
    on public.even_odd_trade_records (external_contract_id)
    where external_contract_id is not null;
create index even_odd_trade_records_sequence_created_idx
    on public.even_odd_trade_records (sequence_id, created_at desc);

alter table public.even_odd_sessions enable row level security;
alter table public.even_odd_sequences enable row level security;
alter table public.even_odd_trade_records enable row level security;

revoke all on table public.even_odd_sessions from anon, authenticated;
revoke all on table public.even_odd_sequences from anon, authenticated;
revoke all on table public.even_odd_trade_records from anon, authenticated;
grant select, insert, update on table public.even_odd_sessions to service_role;
grant select, insert, update on table public.even_odd_sequences to service_role;
grant select, insert, update on table public.even_odd_trade_records to service_role;
