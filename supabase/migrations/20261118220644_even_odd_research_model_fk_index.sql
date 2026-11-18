-- Index the research-run model foreign key for efficient joins and deletes.
create index even_odd_research_runs_model_id_idx
    on public.even_odd_research_runs (model_id);
