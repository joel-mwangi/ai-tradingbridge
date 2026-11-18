-- Require an explicit user-approved monetary session loss cap.
alter table public.even_odd_sessions
    alter column max_session_loss set not null;
