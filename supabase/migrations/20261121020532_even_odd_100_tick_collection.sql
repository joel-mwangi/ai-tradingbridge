-- Permit a 100-tick live browser sample while retaining bounded targets.
alter table public.even_odd_collection_jobs
    drop constraint if exists even_odd_collection_jobs_target_ticks_check;

alter table public.even_odd_collection_jobs
    add constraint even_odd_collection_jobs_target_ticks_check
    check (target_ticks in (100, 500, 1000, 2500, 5000));

comment on column public.even_odd_collection_jobs.target_ticks is
    'User-selected bounded live sample size: 100, 500, 1000, 2500, or 5000 ticks.';
