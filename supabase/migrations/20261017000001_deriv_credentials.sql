-- Migration: deriv credentials and account links
-- Apply with: supabase db push
-- or: psql $DATABASE_URL -f supabase/migrations/20261017000001_deriv_credentials.sql

-- deriv_backend_credentials
-- Stores server-side Fernet-encrypted Deriv OAuth tokens.
-- Only accessible via the service role key; never exposed to browsers.
create table if not exists public.deriv_backend_credentials (
    user_id                 text        primary key,
    encrypted_access_token  text        not null,
    expires_at              timestamptz not null,
    updated_at              timestamptz not null
);

alter table public.deriv_backend_credentials enable row level security;
-- No RLS policies — service role key bypasses RLS entirely.
-- Never grant SELECT/INSERT/UPDATE/DELETE to the anon or authenticated roles.

-- deriv_account_links
-- Maps TradeBridge user IDs to their verified Deriv account IDs.
-- Populated by FastAPI after a successful OAuth exchange; never by the browser.
create table if not exists public.deriv_account_links (
    user_id          text not null,
    deriv_account_id text not null,
    primary key (user_id, deriv_account_id)
);

alter table public.deriv_account_links enable row level security;
-- No RLS policies — service role key bypasses RLS entirely.
-- Never grant SELECT/INSERT/UPDATE/DELETE to the anon or authenticated roles.

create index if not exists deriv_account_links_user_id_idx
    on public.deriv_account_links (user_id);
