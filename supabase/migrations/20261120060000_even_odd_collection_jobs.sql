-- User-controlled, bounded live collection jobs for Even/Odd Lab.
-- Only the backend service role may read or mutate jobs; API ownership is enforced
-- by the authenticated backend proxy before service-role writes.
create table if not exists public.even_odd_collection_jobs (
    id uuid primary key default gen_random_uuid(),
    owner_user_id uuid not null,
    market_symbol text not null check (market_symbol ~ '^[A-Za-z0-9_]{2,32}$'),
    target_ticks integer not null check (target_ticks in (500, 1000, 2500, 5000)),
    received_ticks integer not null default 0 check (received_ticks >= 0 and received_ticks <= target_ticks),
    status text not null default 'queued'
        check (status in ('queued', 'collecting', 'completed', 'stopped', 'failed')),
    worker_id text,
    last_error text,
    created_at timestamptz not null default now(),
    started_at timestamptz,
    completed_at timestamptz,
    updated_at timestamptz not null default now()
);

create index if not exists even_odd_collection_jobs_claim_idx
    on public.even_odd_collection_jobs (status, created_at)
    where status in ('queued', 'collecting');

create index if not exists even_odd_collection_jobs_owner_idx
    on public.even_odd_collection_jobs (owner_user_id, created_at desc);

alter table public.even_odd_collection_jobs enable row level security;
revoke all on public.even_odd_collection_jobs from anon, authenticated;
grant select, insert, update on public.even_odd_collection_jobs to service_role;

create or replace function public.even_odd_claim_collection_job(p_worker_id text)
returns setof public.even_odd_collection_jobs
language plpgsql
security definer
set search_path = public
as $$
declare
    claimed public.even_odd_collection_jobs;
begin
    -- A job already claimed by a worker is not stolen. Worker restarts can
    -- resume queued jobs only; stale collecting jobs require operator review.
    select * into claimed
      from public.even_odd_collection_jobs
     where status = 'queued'
     order by created_at
     for update skip locked
     limit 1;

    if not found then
        return;
    end if;

    update public.even_odd_collection_jobs
       set status = 'collecting',
           worker_id = p_worker_id,
           started_at = coalesce(started_at, now()),
           updated_at = now()
     where id = claimed.id
     returning * into claimed;

    return next claimed;
end;
$$;

revoke all on function public.even_odd_claim_collection_job(text) from public, anon, authenticated;
grant execute on function public.even_odd_claim_collection_job(text) to service_role;
