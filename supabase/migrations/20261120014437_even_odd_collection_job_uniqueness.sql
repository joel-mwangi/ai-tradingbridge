-- Enforce one active collection per owner and market at the database layer.
-- This closes the race between concurrent Start requests.
create unique index if not exists even_odd_one_active_job_per_owner_market
    on public.even_odd_collection_jobs (owner_user_id, market_symbol)
    where status in ('queued', 'collecting');
