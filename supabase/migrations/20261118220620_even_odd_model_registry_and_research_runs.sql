-- Dedicated Even/Odd model registry and experiment history.
-- Apply only to the separate switrade Supabase project.

create table public.even_odd_model_registry (
    id uuid primary key default gen_random_uuid(),
    market_symbol text not null check (market_symbol ~ '^[A-Za-z0-9_]{2,32}$'),
    model_name text not null,
    model_version text not null,
    status text not null default 'candidate'
        check (status in ('candidate', 'validated', 'retired')),
    execution_authorized boolean not null default false,
    model_artifact jsonb not null
        check (jsonb_typeof(model_artifact) = 'object'),
    metrics jsonb not null default '{}'::jsonb
        check (jsonb_typeof(metrics) = 'object'),
    dataset_snapshot_max_id bigint not null check (dataset_snapshot_max_id > 0),
    training_rows integer not null check (training_rows > 0),
    validation_rows integer not null check (validation_rows > 0),
    calibration_rows integer not null check (calibration_rows > 0),
    test_rows integer not null check (test_rows > 0),
    created_at timestamptz not null default now(),
    validated_at timestamptz,
    constraint even_odd_model_version_unique
        unique (market_symbol, model_name, model_version),
    constraint even_odd_model_execution_guard
        check (execution_authorized = false or status = 'validated')
);

create table public.even_odd_research_runs (
    id uuid primary key default gen_random_uuid(),
    market_symbol text not null check (market_symbol ~ '^[A-Za-z0-9_]{2,32}$'),
    snapshot_max_id bigint,
    dataset_rows integer not null default 0 check (dataset_rows >= 0),
    status text not null default 'running'
        check (status in ('running', 'completed', 'failed')),
    quality_report jsonb not null default '{}'::jsonb
        check (jsonb_typeof(quality_report) = 'object'),
    partition_sizes jsonb not null default '{}'::jsonb
        check (jsonb_typeof(partition_sizes) = 'object'),
    evaluation jsonb not null default '{}'::jsonb
        check (jsonb_typeof(evaluation) = 'object'),
    model_id uuid references public.even_odd_model_registry(id) on delete set null,
    error_message text,
    started_at timestamptz not null default now(),
    completed_at timestamptz,
    created_at timestamptz not null default now(),
    constraint even_odd_research_run_completion_check
        check (completed_at is null or completed_at >= started_at)
);

create index even_odd_model_registry_market_created_idx
    on public.even_odd_model_registry (market_symbol, created_at desc);
create index even_odd_research_runs_market_created_idx
    on public.even_odd_research_runs (market_symbol, created_at desc);
create index even_odd_research_runs_status_created_idx
    on public.even_odd_research_runs (status, created_at desc);

alter table public.even_odd_model_registry enable row level security;
alter table public.even_odd_research_runs enable row level security;

revoke all on table public.even_odd_model_registry from anon, authenticated;
revoke all on table public.even_odd_research_runs from anon, authenticated;
grant select, insert, update on table public.even_odd_model_registry to service_role;
grant select, insert, update on table public.even_odd_research_runs to service_role;
