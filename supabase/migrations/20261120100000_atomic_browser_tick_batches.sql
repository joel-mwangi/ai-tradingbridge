-- Make browser-submitted tick batches idempotent and atomic with job progress.
-- Browser observations remain separately labelled and are not mixed into the
-- trusted server live_stream dataset by default.

alter table public.even_odd_ticks
    drop constraint if exists even_odd_ticks_capture_mode_check;

alter table public.even_odd_ticks
    add constraint even_odd_ticks_capture_mode_check
    check (capture_mode in ('historical', 'live_stream', 'browser_stream'));

comment on column public.even_odd_ticks.capture_mode is
    'historical=legacy/imported; live_stream=trusted server collector; browser_stream=authenticated browser capture, separately identifiable and excluded from trusted live_stream reads.';

create table if not exists public.even_odd_collection_batch_receipts (
    job_id uuid not null references public.even_odd_collection_jobs(id) on delete cascade,
    batch_id uuid not null,
    owner_user_id uuid not null,
    collection_id uuid not null references public.even_odd_collection_runs(id),
    tick_count integer not null check (tick_count between 1 and 100),
    payload_hash text not null,
    response jsonb not null check (jsonb_typeof(response) = 'object'),
    created_at timestamptz not null default now(),
    primary key (job_id, batch_id)
);

alter table public.even_odd_collection_batch_receipts enable row level security;
revoke all on public.even_odd_collection_batch_receipts from anon, authenticated;
grant select, insert on public.even_odd_collection_batch_receipts to service_role;

create or replace function public.even_odd_persist_browser_tick_batch(
    p_job_id uuid,
    p_owner_user_id uuid,
    p_batch_id uuid,
    p_ticks jsonb
)
returns jsonb
language sql
security invoker
set search_path = public
as $$
with input as materialized (
    select case when jsonb_typeof(p_ticks) = 'array' then p_ticks else '[]'::jsonb end as ticks
),
payload as materialized (
    select
        count(*)::integer as tick_count,
        md5(coalesce(jsonb_agg(value - 'received_at' - 'capture_id' order by ordinality), '[]'::jsonb)::text) as payload_hash
    from input i
    cross join lateral jsonb_array_elements(i.ticks) with ordinality as item(value, ordinality)
),
validation as materialized (
    select
        count(*)::integer as tick_count,
        coalesce(bool_and(
            (value->>'quote')::numeric > 0
            and (value->>'epoch')::bigint > 0
            and (value->>'pip_size')::smallint between 0 and 10
            and (value->>'last_digit')::smallint between 0 and 9
            and (
                ((value->>'last_digit')::smallint % 2 = 0 and value->>'parity' = 'even')
                or ((value->>'last_digit')::smallint % 2 = 1 and value->>'parity' = 'odd')
            )
            and nullif(value->>'received_at', '') is not null
        ), false) as valid
    from input i
    cross join lateral jsonb_array_elements(i.ticks) with ordinality as item(value, ordinality)
),
job as materialized (
    select *
    from public.even_odd_collection_jobs
    where id = p_job_id and owner_user_id = p_owner_user_id
    for update
),
existing as materialized (
    select r.*
    from public.even_odd_collection_batch_receipts r
    join job j on j.id = r.job_id
    where r.job_id = p_job_id and r.batch_id = p_batch_id
),
new_run as (
    insert into public.even_odd_collection_runs (
        id, market_symbol, status, requested_ticks, received_ticks,
        started_at, completed_at, metadata
    )
    select
        gen_random_uuid(),
        j.market_symbol,
        case when p.tick_count >= 10 then 'completed' else 'partial' end,
        greatest(10, p.tick_count),
        p.tick_count,
        now(),
        now(),
        jsonb_build_object(
            'capture_mode', 'browser_stream',
            'collector', 'authenticated_browser_deriv_public_websocket',
            'job_id', p_job_id,
            'batch_id', p_batch_id,
            'owner_user_id', p_owner_user_id
        )
    from job j
    cross join payload p
    cross join validation v
    where not exists (select 1 from existing)
      and j.status in ('queued', 'collecting')
      and p.tick_count between 1 and 100
      and v.valid
      and v.tick_count = p.tick_count
      and j.received_ticks + p.tick_count <= j.target_ticks
    returning id, market_symbol
),
new_ticks as (
    insert into public.even_odd_ticks (
        capture_id, collection_id, sequence_no, market_symbol,
        tick_epoch, quote, pip_size, last_digit, parity,
        deriv_tick_id, deriv_stream_id, capture_mode, received_at
    )
    select
        gen_random_uuid(),
        nr.id,
        item.ordinality::integer,
        nr.market_symbol,
        (item.value->>'epoch')::bigint,
        (item.value->>'quote')::numeric,
        (item.value->>'pip_size')::smallint,
        (item.value->>'last_digit')::smallint,
        item.value->>'parity',
        nullif(item.value->>'tick_id', ''),
        nullif(item.value->>'stream_id', ''),
        'browser_stream',
        (item.value->>'received_at')::timestamptz
    from new_run nr
    cross join input i
    cross join lateral jsonb_array_elements(i.ticks) with ordinality as item(value, ordinality)
    returning collection_id
),
new_tick_count as (
    select count(*)::integer as tick_count from new_ticks
),
updated_job as (
    update public.even_odd_collection_jobs j
    set received_ticks = j.received_ticks + c.tick_count,
        status = case when j.received_ticks + c.tick_count = j.target_ticks then 'completed' else 'collecting' end,
        started_at = coalesce(j.started_at, now()),
        updated_at = now(),
        completed_at = case when j.received_ticks + c.tick_count = j.target_ticks then now() else null end
    from new_run nr
    cross join new_tick_count c
    where j.id = p_job_id and c.tick_count > 0
    returning j.*
),
receipt_insert as (
    insert into public.even_odd_collection_batch_receipts (
        job_id, batch_id, owner_user_id, collection_id, tick_count, payload_hash, response
    )
    select
        p_job_id,
        p_batch_id,
        p_owner_user_id,
        nr.id,
        c.tick_count,
        p.payload_hash,
        jsonb_build_object(
            'job', to_jsonb(u),
            'persisted_ticks', c.tick_count,
            'collection_id', nr.id,
            'batch_id', p_batch_id,
            'duplicate', false,
            'live_only', true,
            'provenance', 'browser_stream'
        )
    from updated_job u
    cross join new_run nr
    cross join new_tick_count c
    cross join payload p
    returning response
),
duplicate_response as (
    select jsonb_build_object(
        'job', to_jsonb(j),
        'persisted_ticks', e.tick_count,
        'collection_id', e.collection_id,
        'batch_id', p_batch_id,
        'duplicate', true,
        'live_only', true,
        'provenance', 'browser_stream'
    ) as response
    from existing e
    cross join job j
    cross join payload p
    where e.payload_hash = p.payload_hash
),
error_response as (
    select jsonb_build_object(
        'error',
        case
            when p_job_id is null or p_owner_user_id is null or p_batch_id is null then 'job, owner and batch identifiers are required'
            when not exists (select 1 from job) then 'collection job not found'
            when exists (select 1 from existing) and exists (select 1 from existing e cross join payload p where e.payload_hash <> p.payload_hash) then 'batch_id was already used with a different payload'
            when p.tick_count not between 1 and 100 then 'batch must contain between 1 and 100 ticks'
            when not v.valid then 'batch contains invalid tick values'
            when j.status not in ('queued', 'collecting') then 'collection job is no longer active'
            when j.received_ticks + p.tick_count > j.target_ticks then 'batch would exceed the collection target'
            else 'batch was not persisted'
        end
    ) as response
    from payload p
    cross join validation v
    left join job j on true
    where not exists (select 1 from duplicate_response)
      and not exists (select 1 from receipt_insert)
)
select coalesce(
    (select response from duplicate_response),
    (select response from receipt_insert),
    (select response from error_response)
)
$$;

revoke all on function public.even_odd_persist_browser_tick_batch(uuid, uuid, uuid, jsonb)
    from public, anon, authenticated;
grant execute on function public.even_odd_persist_browser_tick_batch(uuid, uuid, uuid, jsonb)
    to service_role;
