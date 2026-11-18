# Even/Odd Lab — Trade Management Specification

## Scope and isolation

The Even/Odd Lab is an independent backend domain. Its code must not import or
reuse the legacy `app.strategy` implementation, existing market-analysis
services, MT5 bridge, or existing strategy execution state. Shared platform
authentication may be integrated later at the API boundary; strategy state and
market-data lifecycle remain separate.

## Approved operating limits

- Sequence length: selected dynamically from 1 through 7 by the ML decision
  pipeline. Seven is a hard maximum, not a target.
- Consecutive losses: stop the active sequence after two consecutive losses.
- Session trades: maximum of ten completed contracts per session.
- Revalidation: required before every contract after the first, and after every
  completed contract before any continuation.
- Stake sizing: fixed stake only.
- Martingale and loss-recovery stake escalation: prohibited.
- Concurrent contracts: one maximum.

## ML sequence-length decision

A validated ML forecasting model supplies a conditional forecast for each
candidate trade in the sequence. Each forecast includes a point estimate of
win probability, a conservative lower confidence bound, actual payout-derived
profit/loss amounts, signal validity, and model identity/version.

The sequence optimizer selects the longest contiguous prefix for which each
candidate has positive conservative expected value:

`EV_lower = p_lower * profit_if_win - (1 - p_lower) * loss_if_lose`

The optimizer refuses to authorize trades if the model is not validated, the
forecast sequence is incomplete/out of order, the signal is invalid, or
conservative expected value is non-positive. The selected length is a planned
upper bound only. It does not authorize blind execution of all selected trades.

This initial optimizer is a deterministic decision layer consuming ML outputs;
it is not itself a trained ML model. A trained sequence policy must be
developed and evaluated with chronological splits and walk-forward validation.
It must demonstrate incremental out-of-sample value over simple baselines
before it is permitted to control demo execution.

## Revalidation and stopping

Before every contract, require:
- validated model and current model version;
- fresh and correctly ordered market data;
- still-valid opportunity and entry conditions;
- positive conservative expected value using current payout terms;
- available risk budget;
- remaining sequence and session capacity.

Stop immediately when any condition fails, when two consecutive losses occur,
when the selected sequence length is reached, or when ten session contracts
have completed. A win resets only the consecutive-loss counter; it does not
reset session exposure or session trade count.

## Risk boundary

Risk controls are hard constraints and can veto ML recommendations. Monetary
stake and session-loss caps must be configured from the account balance and
user-approved risk tolerance before execution. No monetary default is silently
assumed. If the minimum permitted contract stake exceeds the configured risk
cap, reject the trade.

## Research and execution boundary

The current implementation provides policy validation, sequence-length
selection from supplied forecasts, and in-memory sequence/session guardrails.
It does not connect to Deriv, persist sessions, train models, or place orders.
Those capabilities require separate milestones and tests.
