-- Keep the public-schema RLS auto-enable event trigger, but do not expose
-- direct RPC execution to anon/authenticated roles.
revoke all on function public.rls_auto_enable()
    from public, anon, authenticated;
