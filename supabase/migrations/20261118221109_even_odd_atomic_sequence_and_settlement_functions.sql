-- Add atomic sequence creation and trade settlement functions.
-- SECURITY INVOKER and restricted EXECUTE grants keep the functions backend-only.

alter table public.even_odd_sessions
    add column market_symbol text not null
        check (market_symbol ~ '^[A-Za-z0-9_]{2,32}$');

create function public.even_odd_start_sequence(
    p_session_id uuid,
    p_model_id uuid,
    p_model_version text,
    p_planned_trades integer,
    p_expected_value_snapshot jsonb default '{}'::jsonb
)
returns jsonb
language plpgsql
security invoker
set search_path = ''
as $function$
declare
    v_session public.even_odd_sessions%rowtype;
    v_model public.even_odd_model_registry%rowtype;
    v_sequence public.even_odd_sequences%rowtype;
begin
    if p_planned_trades is null or p_planned_trades < 1 or p_planned_trades > 7 then
        raise exception 'Planned sequence length must be between 1 and 7';
    end if;
    if p_expected_value_snapshot is null
       or jsonb_typeof(p_expected_value_snapshot) <> 'object' then
        raise exception 'Expected-value snapshot must be a JSON object';
    end if;

    select * into v_session
    from public.even_odd_sessions
    where id = p_session_id
    for update;
    if not found then raise exception 'Session not found'; end if;
    if v_session.status <> 'active' then raise exception 'Session is closed'; end if;
    if v_session.completed_trades >= v_session.max_trades_per_session then
        raise exception 'Session trade limit has been reached';
    end if;
    if p_planned_trades > v_session.max_trades_per_session - v_session.completed_trades then
        raise exception 'Planned sequence exceeds remaining session capacity';
    end if;

    select * into v_model
    from public.even_odd_model_registry
    where id = p_model_id
    for share;
    if not found then raise exception 'Model not found'; end if;
    if v_model.status <> 'validated' then
        raise exception 'Only independently validated models may start a sequence';
    end if;
    if v_model.model_version <> p_model_version
       or v_model.market_symbol <> v_session.market_symbol then
        raise exception 'Model version or market does not match the session';
    end if;

    if exists (
        select 1 from public.even_odd_sequences
        where session_id = p_session_id and status = 'active'
    ) then
        raise exception 'An active sequence already exists for this session';
    end if;

    -- Starting a new sequence is an explicit restart after a prior sequence stop.
    update public.even_odd_sessions
    set consecutive_losses = 0
    where id = p_session_id;

    insert into public.even_odd_sequences (
        session_id, model_id, model_version, planned_trades,
        status, expected_value_snapshot
    )
    values (
        p_session_id, p_model_id, p_model_version, p_planned_trades,
        'active', p_expected_value_snapshot
    )
    returning * into v_sequence;

    return jsonb_build_object(
        'sequence_id', v_sequence.id,
        'session_id', v_sequence.session_id,
        'model_id', v_sequence.model_id,
        'model_version', v_sequence.model_version,
        'planned_trades', v_sequence.planned_trades,
        'status', v_sequence.status,
        'execution_authorized', false
    );
end;
$function$;

create function public.even_odd_record_trade_settlement(
    p_sequence_id uuid,
    p_trade_number integer,
    p_side text,
    p_stake numeric,
    p_profit_if_win numeric,
    p_loss_if_lose numeric,
    p_even_probability numeric,
    p_lower_even_probability numeric,
    p_upper_even_probability numeric,
    p_conservative_expected_value numeric,
    p_revalidation jsonb,
    p_outcome text,
    p_realized_profit numeric,
    p_external_contract_id text default null
)
returns jsonb
language plpgsql
security invoker
set search_path = ''
as $function$
declare
    v_sequence public.even_odd_sequences%rowtype;
    v_session public.even_odd_sessions%rowtype;
    v_model public.even_odd_model_registry%rowtype;
    v_probability_lower numeric;
    v_break_even numeric;
    v_calculated_ev numeric;
    v_trade_id uuid;
    v_new_session_trades integer;
    v_new_session_wins integer;
    v_new_session_losses integer;
    v_new_session_consecutive integer;
    v_new_session_profit numeric;
    v_new_sequence_trades integer;
    v_new_sequence_wins integer;
    v_new_sequence_losses integer;
    v_new_sequence_consecutive integer;
    v_sequence_status text := 'active';
    v_session_status text := 'active';
    v_stop_reason text;
begin
    if p_side not in ('even', 'odd') then
        raise exception 'Side must be even or odd';
    end if;
    if p_outcome not in ('win', 'loss') then
        raise exception 'Outcome must be win or loss';
    end if;
    if p_revalidation is null or jsonb_typeof(p_revalidation) <> 'object' then
        raise exception 'Revalidation evidence must be a JSON object';
    end if;
    if p_revalidation->>'model_validated' is distinct from 'true'
       or p_revalidation->>'uncertainty_validated' is distinct from 'true'
       or p_revalidation->>'signal_valid' is distinct from 'true'
       or p_revalidation->>'data_fresh' is distinct from 'true'
       or p_revalidation->>'risk_budget_available' is distinct from 'true' then
        raise exception 'Trade revalidation gates have not all passed';
    end if;
    if p_stake is null or p_stake <= 0
       or p_profit_if_win is null or p_profit_if_win <= 0
       or p_loss_if_lose is null or p_loss_if_lose <= 0 then
        raise exception 'Stake and payout terms must be positive';
    end if;
    if p_even_probability is null or p_even_probability < 0 or p_even_probability > 1
       or p_lower_even_probability is null or p_lower_even_probability < 0
       or p_upper_even_probability is null or p_upper_even_probability > 1
       or p_lower_even_probability > p_even_probability
       or p_even_probability > p_upper_even_probability then
        raise exception 'Probability interval is invalid';
    end if;
    if p_realized_profit is null
       or (p_outcome = 'win' and p_realized_profit <= 0)
       or (p_outcome = 'loss' and p_realized_profit >= 0) then
        raise exception 'Realized profit sign does not match the settlement outcome';
    end if;

    select * into v_sequence
    from public.even_odd_sequences
    where id = p_sequence_id
    for update;
    if not found then raise exception 'Sequence not found'; end if;
    select * into v_session
    from public.even_odd_sessions
    where id = v_sequence.session_id
    for update;
    if not found then raise exception 'Session not found'; end if;

    if v_sequence.status <> 'active' or v_session.status <> 'active' then
        raise exception 'Sequence or session is not active';
    end if;
    if p_trade_number <> v_sequence.completed_trades + 1 then
        raise exception 'Trade number is not the next expected sequence number';
    end if;
    if v_sequence.completed_trades >= v_sequence.planned_trades then
        raise exception 'The planned sequence is already complete';
    end if;
    if v_session.completed_trades >= v_session.max_trades_per_session then
        raise exception 'Session trade limit has been reached';
    end if;
    if v_sequence.consecutive_losses >= v_session.max_consecutive_losses then
        raise exception 'Consecutive-loss limit has been reached';
    end if;
    if p_stake <> v_session.fixed_stake then
        raise exception 'Stake must remain fixed for the entire session';
    end if;

    select * into v_model
    from public.even_odd_model_registry
    where id = v_sequence.model_id
    for share;
    if not found or v_model.status <> 'validated'
       or v_model.model_version <> v_sequence.model_version
       or v_model.market_symbol <> v_session.market_symbol then
        raise exception 'Sequence model is no longer valid for this session';
    end if;

    if p_side = 'even' then
        v_probability_lower := p_lower_even_probability;
    else
        v_probability_lower := 1 - p_upper_even_probability;
    end if;
    v_break_even := p_loss_if_lose / (p_profit_if_win + p_loss_if_lose);
    v_calculated_ev := (
        v_probability_lower * p_profit_if_win
        - (1 - v_probability_lower) * p_loss_if_lose
    );
    if v_probability_lower <= v_break_even or v_calculated_ev <= 0 then
        raise exception 'Conservative payout-adjusted edge is not positive';
    end if;
    if p_conservative_expected_value is null
       or abs(p_conservative_expected_value - v_calculated_ev) > 0.000001 then
        raise exception 'Supplied conservative expected value does not match payout terms';
    end if;

    insert into public.even_odd_trade_records (
        sequence_id, trade_number, side, stake, profit_if_win, loss_if_lose,
        even_probability, lower_even_probability, upper_even_probability,
        conservative_expected_value, revalidation, status, outcome,
        realized_profit, external_contract_id, settled_at
    )
    values (
        p_sequence_id, p_trade_number, p_side, p_stake, p_profit_if_win, p_loss_if_lose,
        p_even_probability, p_lower_even_probability, p_upper_even_probability,
        v_calculated_ev, p_revalidation, 'settled', p_outcome,
        p_realized_profit, p_external_contract_id, now()
    )
    returning id into v_trade_id;

    v_new_sequence_trades := v_sequence.completed_trades + 1;
    v_new_sequence_wins := v_sequence.wins + case when p_outcome = 'win' then 1 else 0 end;
    v_new_sequence_losses := v_sequence.losses + case when p_outcome = 'loss' then 1 else 0 end;
    v_new_sequence_consecutive := case
        when p_outcome = 'win' then 0
        else v_sequence.consecutive_losses + 1
    end;

    v_new_session_trades := v_session.completed_trades + 1;
    v_new_session_wins := v_session.wins + case when p_outcome = 'win' then 1 else 0 end;
    v_new_session_losses := v_session.losses + case when p_outcome = 'loss' then 1 else 0 end;
    v_new_session_consecutive := case
        when p_outcome = 'win' then 0
        else v_session.consecutive_losses + 1
    end;
    v_new_session_profit := v_session.realized_profit + p_realized_profit;

    if v_new_sequence_consecutive >= v_session.max_consecutive_losses then
        v_sequence_status := 'stopped';
        v_stop_reason := 'Consecutive-loss limit reached';
    elsif v_new_sequence_trades >= v_sequence.planned_trades then
        v_sequence_status := 'completed';
        v_stop_reason := 'ML-selected sequence length completed';
    end if;

    if v_new_session_trades >= v_session.max_trades_per_session then
        v_session_status := 'closed';
        v_stop_reason := 'Session trade limit reached';
    elsif v_session.max_session_loss is not null
       and v_new_session_profit <= -v_session.max_session_loss then
        v_session_status := 'closed';
        v_stop_reason := 'Session loss limit reached';
    end if;

    if v_session_status = 'closed' and v_sequence_status = 'active' then
        v_sequence_status := 'stopped';
    end if;

    update public.even_odd_sequences
    set completed_trades = v_new_sequence_trades,
        wins = v_new_sequence_wins,
        losses = v_new_sequence_losses,
        consecutive_losses = v_new_sequence_consecutive,
        status = v_sequence_status,
        stop_reason = case when v_sequence_status = 'active' then null else v_stop_reason end,
        completed_at = case when v_sequence_status = 'active' then null else now() end
    where id = p_sequence_id;

    update public.even_odd_sessions
    set completed_trades = v_new_session_trades,
        wins = v_new_session_wins,
        losses = v_new_session_losses,
        consecutive_losses = least(v_new_session_consecutive, 2),
        realized_profit = v_new_session_profit,
        status = v_session_status,
        closed_at = case when v_session_status = 'closed' then now() else null end
    where id = v_session.id;

    return jsonb_build_object(
        'trade_id', v_trade_id,
        'sequence_id', p_sequence_id,
        'sequence_status', v_sequence_status,
        'session_id', v_session.id,
        'session_status', v_session_status,
        'completed_trades_in_sequence', v_new_sequence_trades,
        'completed_trades_in_session', v_new_session_trades,
        'consecutive_losses', v_new_sequence_consecutive,
        'stop_reason', v_stop_reason,
        'execution_authorized', false
    );
end;
$function$;

revoke all on function public.even_odd_start_sequence(uuid, uuid, text, integer, jsonb)
    from public, anon, authenticated;
grant execute on function public.even_odd_start_sequence(uuid, uuid, text, integer, jsonb)
    to service_role;

revoke all on function public.even_odd_record_trade_settlement(
    uuid, integer, text, numeric, numeric, numeric, numeric, numeric, numeric,
    numeric, jsonb, text, numeric, text
) from public, anon, authenticated;
grant execute on function public.even_odd_record_trade_settlement(
    uuid, integer, text, numeric, numeric, numeric, numeric, numeric, numeric,
    numeric, jsonb, text, numeric, text
) to service_role;
