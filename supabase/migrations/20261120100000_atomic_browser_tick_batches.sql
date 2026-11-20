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
language plpgsql
security invoker
set search_path = public
as $$
declare
    v_job public.even_odd_collection_jobs;
    v_existing public.even_odd_collection_batch_receipts;
    v_collection_id uuid;
    v_count integer;
    v_received integer;
    v_status text;
    v_result jsonb;
    v_tick jsonb;
    v_index integer := 0;
    v_quote numeric;
    v_epoch bigint;
    v_pip_size smallint;
    v_last_digit smallint;
    v_parity text;
    v_received_at timestamptz;
    v_payload_hash text;
begin
    if p_job_id is null or p_owner_user_id is null or p_batch_id is null then
        raise exception 'job, owner and batch identifiers are required' using errcode = '22023';
    end if;
    if jsonb_typeof(p_ticks) <> 'array' then
        raise exception 'ticks must be a JSON array' using errcode = '22023';
    end if;
    v_count := jsonb_array_length(p_ticks);
    if v_count < 1 or v_count > 100 then
        raise exception 'batch must contain between 1 and 100 ticks' using errcode = '22023';
    end if;

    select * into v_job
      from public.even_odd_collection_jobs
     where id = p_job_id and owner_user_id = p_owner_user_id
     for update;
    if not found then
        raise exception 'collection job not found' using errcode = 'P0002';
    end if;

    -- Exclude backend-generated receive timestamps and random capture IDs:
    -- those change when an identical raw browser batch is retried.
    select md5(jsonb_agg(value - 'received_at' - 'capture_id' order by ordinality)::text)
      into v_payload_hash
      from jsonb_array_elements(p_ticks) with ordinality as item(value, ordinality);

    -- Idempotency is checked after taking the job lock so concurrent retries
    -- serialize and can only commit one receipt.
    select * into v_existing
      from public.even_odd_collection_batch_receipts
     where job_id = p_job_id and batch_id = p_batch_id;
    if found then
        if v_existing.owner_user_id <> p_owner_user_id then
            raise exception 'batch owner mismatch' using errcode = '42501';
        end if;
        if v_existing.payload_hash <> v_payload_hash then
            raise exception 'batch_id was already used with a different payload' using errcode = '22023';
        end if;
        return jsonb_build_object(
            'job', to_jsonb(v_job),
            'persisted_ticks', v_existing.tick_count,
            'collection_id', v_existing.collection_id,
            'batch_id', p_batch_id,
            'duplicate', true,
            'live_only', true,
            'provenance', 'browser_stream'
        );
    end if;

    if v_job.status not in ('queued', 'collecting') then
        raise exception 'collection job is no longer active' using errcode = '55000';
    end if;
    if v_job.received_ticks + v_count > v_job.target_ticks then
        raise exception 'batch would exceed the collection target' using errcode = '22023';
    end if;

    v_collection_id := gen_random_uuid();
    insert into public.even_odd_collection_runs (
        id, market_symbol, status, requested_ticks, received_ticks,
        started_at, completed_at, metadata
    ) values (
        v_collection_id, v_job.market_symbol,
        case when v_count >= 10 then 'completed' else 'partial' end,
        greatest(10, v_count), v_count, now(), now(),
        jsonb_build_object(
            'capture_mode', 'browser_stream',
            'collector', 'authenticated_browser_deriv_public_websocket',
            'job_id', p_job_id,
            'batch_id', p_batch_id,
            'owner_user_id', p_owner_user_id
        )
    );

    for v_tick in select value from jsonb_array_elements(p_ticks)
    loop
        v_index := v_index + 1;
        v_quote := (v_tick->>'quote')::numeric;
        v_epoch := (v_tick->>'epoch')::bigint;
        v_pip_size := (v_tick->>'pip_size')::smallint;
        v_last_digit := (v_tick->>'last_digit')::smallint;
        v_parity := v_tick->>'parity';
        v_received_at := (v_tick->>'received_at')::timestamptz;

        if v_quote <= 0 or v_epoch <= 0 or v_pip_size not between 0 and 10
           or v_last_digit not between 0 and 9
           or v_parity not in ('even', 'odd')
           or v_parity <> case when v_last_digit % 2 = 0 then 'even' else 'odd' end
           or v_received_at is null then
            raise exception 'invalid normalized tick at batch index %', v_index using errcode = '22023';
        end if;

        insert into public.even_odd_ticks (
            capture_id, collection_id, sequence_no, market_symbol,
            tick_epoch, quote, pip_size, last_digit, parity,
            deriv_tick_id, deriv_stream_id, capture_mode, received_at
        ) values (
            gen_random_uuid(), v_collection_id, v_index, v_job.market_symbol,
            v_epoch, v_quote, v_pip_size, v_last_digit, v_parity,
            nullif(v_tick->>'tick_id', ''),
            nullif(v_tick->>'stream_id', ''),
            'browser_stream', v_received_at
        );
    end loop;

    v_received := v_job.received_ticks + v_count;
    v_status := case when v_received = v_job.target_ticks then 'completed' else 'collecting' end;

    update public.even_odd_collection_jobs
       set received_ticks = v_received,
           status = v_status,
           started_at = coalesce(started_at, now()),
           updated_at = now(),
           completed_at = case when v_status = 'completed' then now() else null end
     where id = p_job_id
     returning * into v_job;

    v_result := jsonb_build_object(
        'job', to_jsonb(v_job),
        'persisted_ticks', v_count,
        'collection_id', v_collection_id,
        'batch_id', p_batch_id,
        'duplicate', false,
        'live_only', true,
        'provenance', 'browser_stream'
    );

    insert into public.even_odd_collection_batch_receipts (
        job_id, batch_id, owner_user_id, collection_id, tick_count, payload_hash, response
    ) values (
        p_job_id, p_batch_id, p_owner_user_id, v_collection_id, v_count, v_payload_hash, v_result
    );

    return v_result;
end;
$$;

revoke all on function public.even_odd_persist_browser_tick_batch(uuid, uuid, uuid, jsonb)
    from public, anon, authenticated;
grant execute on function public.even_odd_persist_browser_tick_batch(uuid, uuid, uuid, jsonb)
    to service_role;
