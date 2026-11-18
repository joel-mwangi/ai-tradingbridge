-- Correct the semantics of Deriv's tick.id field without interrupting either
-- the currently deployed backend or the updated backend during rollout.
-- Deriv's tick.id is a per-connection stream identifier, not a unique tick ID.

alter table public.even_odd_ticks
    add column deriv_stream_id text;

update public.even_odd_ticks
set deriv_stream_id = deriv_tick_id
where deriv_stream_id is null
  and deriv_tick_id is not null;

comment on column public.even_odd_ticks.deriv_stream_id is
    'Deriv per-connection stream identifier; shared by ticks in the same subscription and not unique per tick.';

comment on column public.even_odd_ticks.deriv_tick_id is
    'Deprecated legacy column retained temporarily for backward-compatible deployment; values mirror deriv_stream_id for existing rows.';
