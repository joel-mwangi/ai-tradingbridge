-- Reclaim a collection only after its worker lease has expired.
-- The worker renews updated_at at least every five seconds while the socket is quiet.
create or replace function public.even_odd_claim_collection_job(p_worker_id text)
returns setof public.even_odd_collection_jobs
language plpgsql
security definer
set search_path = public
as $$
declare
    claimed public.even_odd_collection_jobs;
begin
    select * into claimed
      from public.even_odd_collection_jobs
     where status = 'queued'
        or (status = 'collecting' and updated_at < now() - interval '120 seconds')
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
