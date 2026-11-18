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

Risk controls are hard constraints and can veto ML recommendations. Every
persistent session requires a fixed stake, currency, and explicit user-approved
maximum session loss. No monetary risk cap is silently assumed. The database
rejects any settlement whose stake differs from the session's fixed stake.
The database checks the remaining loss budget before inserting a settled
trade record, then closes the session if its completed-trade cap or realized
loss cap is reached. If the proposed loss would exceed the remaining budget,
the settlement record is rejected. If the minimum permitted contract stake
exceeds the configured risk cap, reject the trade.

## Persistent state and atomic transitions

Session, sequence, and settled-trade records are stored in the dedicated
Even/Odd Supabase project. A database transaction locks the session and
sequence rows while recording each settlement, then updates the counters and
terminal state atomically. This prevents concurrent requests from bypassing
the seven-trade sequence limit, ten-trade session limit, fixed-stake rule, or
two-consecutive-loss stop. Only one active sequence is permitted per session.

Starting a sequence requires a model registry entry marked validated with the
same market and version as the session. Recording a settlement rechecks the
model status, probability interval, payout-derived conservative expected
value, fixed stake, fresh-data/signal/risk revalidation flags, and trade order.
A new sequence is an explicit restart and resets the sequence-level loss streak.

These APIs only create session records, start research sequences, and record
settled outcomes. They do not submit Deriv orders. All responses explicitly
keep order execution disabled.

## Research and execution boundary

The backend includes chronological feature generation, candidate-model
training, calibration, holdout evaluation, expanding walk-forward evaluation,
baseline comparison, and uncertainty estimation. Research results and model
artifacts are persisted as candidates. There is no automatic model promotion;
a model must pass the documented out-of-sample gates before an authorized
operator can mark it validated.

The entry-check API verifies persisted tick identity and freshness, computes
probabilities on the server from history preceding each referenced tick,
retrieves fresh one-tick `DIGITEVEN`/`DIGITODD` proposal prices from Deriv,
and applies server-owned confirmation, uncertainty, break-even, conservative
EV, and session-risk thresholds. Caller-supplied probabilities, payouts, data
ages, and relaxed thresholds are not trusted.

The payout proposal is read-only. The backend records session/sequence state
and settled outcomes but does not buy proposals or submit Deriv orders. The
currently verified database sample is too small to establish predictive edge;
execution remains disabled until adequate out-of-sample evidence and final
production verification are available.
