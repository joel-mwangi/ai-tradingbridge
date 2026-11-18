-- Correct the semantics of Deriv's tick.id field without losing existing data.
-- Deriv documents tick.id as a per-connection stream identifier, not a unique tick ID.

alter table public.even_odd_ticks
    rename column deriv_tick_id to deriv_stream_id;

comment on column public.even_odd_ticks.deriv_stream_id is
    'Deriv per-connection stream identifier; shared by ticks in the same subscription and not unique per tick.';
