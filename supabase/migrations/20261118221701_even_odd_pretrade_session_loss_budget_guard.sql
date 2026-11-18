-- Enforce the remaining session loss budget before any settled trade record.
create function public.even_odd_enforce_session_risk_budget()
returns trigger
language plpgsql
security invoker
set search_path = ''
as $function$
declare
    v_session public.even_odd_sessions%rowtype;
begin
    select s.* into v_session
    from public.even_odd_sessions s
    join public.even_odd_sequences q on q.session_id = s.id
    where q.id = new.sequence_id
    for update of s;

    if not found then
        raise exception 'Trade sequence session was not found';
    end if;
    if new.stake <> v_session.fixed_stake then
        raise exception 'Trade stake must match the session fixed stake';
    end if;
    if v_session.realized_profit - new.loss_if_lose < -v_session.max_session_loss then
        raise exception 'Insufficient remaining session loss budget for this trade';
    end if;
    return new;
end;
$function$;

create trigger even_odd_trade_session_risk_budget_guard
before insert on public.even_odd_trade_records
for each row
execute function public.even_odd_enforce_session_risk_budget();

revoke all on function public.even_odd_enforce_session_risk_budget()
    from public, anon, authenticated;
grant execute on function public.even_odd_enforce_session_risk_budget()
    to service_role;
