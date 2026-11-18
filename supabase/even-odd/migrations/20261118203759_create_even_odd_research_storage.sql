-- Dedicated Even/Odd Lab research storage.
-- Apply only to the separate switrade Supabase project.

create table public.even_odd_collection_runs (
    id uuid primary key default gen_random_uuid(),
    market_symbol text not null check (market_symbol ~ '^[A-Za-z0-9_]{2,32}$'),
    source text not null default 'deriv_public_ws' check (source = 'deriv_public_ws'),
    status text not null default 'collecting'
        check (status in ('collecting', 'completed', 'partial', 'failed')),
    requested_ticks integer not null check (requested_ticks between 10 and 300),
    received_ticks integer not null default 0 check (received_ticks between 0 and 300),
    started_at timestamptz not null default now(),
    completed_at timestamptz,
    error_message text,
    metadata jsonb not null default '{}'::jsonb
        check (jsonb_typeof(metadata) = 'object'),
    created_at timestamptz not null default now(),
    constraint even_odd_collection_run_completion_check
        check (completed_at is null or completed_at >= started_at)
);

create table public.even_odd_ticks (
    id bigint generated always as identity primary key,
    collection_id uuid not null
        references public.even_odd_collection_runs(id) on delete cascade,
    sequence_no integer not null check (sequence_no > 0),
    market_symbol text not null check (market_symbol ~ '^[A-Za-z0-9_]{2,32}$'),
    tick_epoch bigint not null check (tick_epoch > 0),
    quote numeric(30, 10) not null check (quote > 0),
    pip_size smallint not null check (pip_size between 0 and 10),
    last_digit smallint not null check (last_digit between 0 and 9),
    parity text not null check (parity in ('even', 'odd')),
    deriv_tick_id text,
    received_at timestamptz not null,
    created_at timestamptz not null default now(),
    constraint even_odd_tick_parity_matches_digit
        check (parity = case when last_digit % 2 = 0 then 'even' else 'odd' end),
    constraint even_odd_tick_sequence_unique
        unique (collection_id, sequence_no)
);

create index even_odd_collection_runs_market_started_idx
    on public.even_odd_collection_runs (market_symbol, started_at desc);
create index even_odd_ticks_market_epoch_idx
    on public.even_odd_ticks (market_symbol, tick_epoch desc);
create index even_odd_ticks_collection_sequence_idx
    on public.even_odd_ticks (collection_id, sequence_no);

alter table public.even_odd_collection_runs enable row level security;
alter table public.even_odd_ticks enable row level security;

revoke all on table public.even_odd_collection_runs from anon, authenticated;
revoke all on table public.even_odd_ticks from anon, authenticated;
grant select, insert, update, delete on table public.even_odd_collection_runs to service_role;
grant select, insert, update, delete on table public.even_odd_ticks to service_role;
grant usage, select on sequence public.even_odd_ticks_id_seq to service_role;
