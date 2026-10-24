# End-to-end strategy workflow acceptance

This checklist is paired with the frontend contract at docs/END_TO_END_STRATEGY_WORKFLOW.md in the joel-mwangi/tradebridge repository.

## Authority boundaries
- FastAPI's LiveStrategyFeed is the authority for market observation, frequency qualification, stability, percentage trigger, sequence tracking, and signal generation.
- The frontend is a consumer of strategy status and may execute only when the status matches the exact selected market, direction, barrier, and frequency window.
- The strategy feed remains observation/signal-only. It must not submit Deriv contracts.
- The frontend's demo execution coordinator may start at most three sequential Demo contracts after one signal. Each must be settled and reconciled before the next.
- Unknown/rejected/unresolved contract outcomes require reconciliation and must never trigger automatic retry.
- Real-account auto-execution remains disabled.

## Acceptance test matrix
- Selected direction, barrier, symbol, and window survive Run -> status -> signal without default substitution.
- Non-qualifying setup never emits an executable signal.
- A changed Red/Green identity resets stability and pending entry state.
- Duplicate/out-of-order/invalid feed events block the active strategy rather than creating a signal.
- A single completed sequence emits one signal; repeated status frames do not create a second execution sequence.
- Demo sequence stops after two consecutive settled losses, resets the loss streak on win/breakeven, and does not submit trade three after two losses.
- Provider timeout, ambiguous buy, missing settlement, or disconnect enters reconciliation-required state.
- Actual Deriv Demo integration and deployed frontend/backend compatibility are required before claiming end-to-end completion.

## Current limitation
Python strategy tests and frontend unit tests are not a substitute for a live-provider Demo integration run. No end-to-end Deriv Demo execution is certified by this document.
