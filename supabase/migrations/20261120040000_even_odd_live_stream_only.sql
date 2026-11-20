-- Live-only Even/Odd research capture.
-- Existing imported observations remain archived but are excluded from research
-- reads by capture_mode='live_stream'. No historical rows are deleted.

alter table public.even_odd_ticks
    add column if not exists capture_mode text not null default 'historical';

alter table public.even_odd_ticks
    add constraint even_odd_ticks_capture_mode_check
    check (capture_mode in ('historical', 'live_stream'));

create index if not exists even_odd_ticks_live_market_epoch_idx
    on public.even_odd_ticks (market_symbol, tick_epoch desc, id desc)
    where capture_mode = 'live_stream';

create table if not exists public.even_odd_live_heartbeats (
    market_symbol text primary key
        check (market_symbol ~ '^[A-Za-z0-9_]{2,32}$'),
    worker_id text not null,
    status text not null
        check (status in ('connecting', 'connected', 'reconnecting', 'stopped', 'error')),
    started_at timestamptz not null,
    last_tick_at timestamptz,
    ticks_received bigint not null default 0 check (ticks_received >= 0),
    last_error text,
    updated_at timestamptz not null default now()
);

alter table public.even_odd_live_heartbeats enable row level security;
revoke all on table public.even_odd_live_heartbeats from anon, authenticated;
grant select, insert, update on table public.even_odd_live_heartbeats to service_role;

comment on column public.even_odd_ticks.capture_mode is
    'historical rows are legacy/imported and excluded from Even/Odd research; live_stream rows were captured after a Deriv ticks subscription began.';
comment on table public.even_odd_live_heartbeats is
    'Operational heartbeat for the independent always-on Deriv live tick collector.';
