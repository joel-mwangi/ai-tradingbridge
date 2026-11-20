-- Keep the identity sequence private to the backend service role.
revoke all on sequence public.even_odd_payout_quote_snapshots_id_seq from anon, authenticated;
grant usage, select on sequence public.even_odd_payout_quote_snapshots_id_seq to service_role;
