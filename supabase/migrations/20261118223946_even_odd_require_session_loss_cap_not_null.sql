-- A missing session loss cap would make the risk-budget comparison evaluate to NULL.
-- Require an explicit positive cap at the database boundary, not only in the API.
alter table public.even_odd_sessions
    alter column max_session_loss set not null;
