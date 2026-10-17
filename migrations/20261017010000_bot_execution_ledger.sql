-- Durable ledger for the future server-owned Demo bot worker.
-- Apply with the Supabase migration workflow for the project used by TradeBridge.
-- RLS is enabled with no client policies: only the backend service role may access rows.
create table if not exists public.bot_runs (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  account_id text not null,
  bot_id text not null,
  config jsonb not null,
  status text not null check (status in (
    'queued', 'running', 'stopping', 'stopped', 'completed', 'failed', 'interrupted', 'reconciliation_required'
  )),
  stop_reason text,
  trade_count integer not null default 0 check (trade_count >= 0),
  wins integer not null default 0 check (wins >= 0),
  losses integer not null default 0 check (losses >= 0),
  realized_pnl numeric(18, 8) not null default 0,
  currency text not null default 'USD',
  started_at timestamptz,
  ended_at timestamptz,
  heartbeat_at timestamptz,
  version bigint not null default 0,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create unique index if not exists bot_runs_one_active_per_bot_account
  on public.bot_runs(user_id, account_id, bot_id)
  where status in ('queued', 'running', 'stopping', 'reconciliation_required');

create index if not exists bot_runs_user_created_at
  on public.bot_runs(user_id, created_at desc);

create table if not exists public.bot_trades (
  id uuid primary key default gen_random_uuid(),
  run_id uuid not null references public.bot_runs(id) on delete cascade,
  user_id uuid not null references auth.users(id) on delete cascade,
  account_id text not null,
  idempotency_key text not null,
  request_id bigint,
  proposal_id text,
  contract_id text,
  state text not null check (state in (
    'created', 'requesting_proposal', 'buy_pending', 'open', 'settling',
    'settled', 'failed', 'outcome_unknown'
  )),
  contract_type text not null,
  symbol text not null,
  stake numeric(18, 8) not null check (stake > 0),
  payout numeric(18, 8),
  profit numeric(18, 8),
  currency text not null default 'USD',
  broker_status text,
  error_code text,
  error_message text,
  proposal_requested_at timestamptz,
  buy_requested_at timestamptz,
  opened_at timestamptz,
  settled_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique(run_id, idempotency_key)
);

create unique index if not exists bot_trades_unique_contract_per_account
  on public.bot_trades(account_id, contract_id)
  where contract_id is not null;

create index if not exists bot_trades_run_created_at
  on public.bot_trades(run_id, created_at desc);

alter table public.bot_runs enable row level security;
alter table public.bot_trades enable row level security;

comment on table public.bot_runs is
  'Server-owned Demo bot run state; browser local storage is not authoritative.';
comment on table public.bot_trades is
  'Durable broker order ledger. Settled P&L is recorded only from confirmed Deriv contract updates.';


-- Worker liveness is durable and server-authoritative. Run creation fails closed if no
-- persistent worker has checked in recently.
create table if not exists public.bot_worker_heartbeats (
  worker_id text primary key,
  last_seen_at timestamptz not null,
  started_at timestamptz not null default now()
);
alter table public.bot_worker_heartbeats enable row level security;
comment on table public.bot_worker_heartbeats is
  'Private liveness registry for persistent bot execution workers; service role only.';
