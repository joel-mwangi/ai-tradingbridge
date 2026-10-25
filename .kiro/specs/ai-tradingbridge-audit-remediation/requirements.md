# Requirements Document

## Introduction

This document captures remediation requirements for the AI-TradeBridge backend identified during an audit of the digit strategy subsystem. The audit found six categories of defects: failing CI tests with incorrect fixtures, an unguarded active-run lifecycle in the WebSocket gateway, unsafe handling of unknown order outcomes, unresolved specification ambiguities between documentation and code, an overly permissive window-size contract, and ad-hoc settlement normalization that bypasses the canonical `contract_normalization.py` module. Work is sequenced from highest to lowest priority: CI green first, then P1 items in the order: active-run lifecycle → safe order reconciliation → strategy spec resolution → percentage-trigger/window contract → centralized settlement normalization. The system targets demo accounts only; real-account auto-execution must remain disabled.

## Glossary

- **CI Fix**: Repair of an existing test whose fixture or assertion does not match the intended behavior, making the test pass for the correct reason.
- **DigitStrategySession**: The side-effect-free state machine in `app/strategy/session.py` that tracks market stability, qualification, and signal generation.
- **StrategyExecution**: The demo-only sequential execution coordinator in `app/strategy/execution.py` that manages up to three sequential buy/settle cycles.
- **LiveStrategyFeed**: The per-WebSocket bridge in `app/strategy/live_feed.py` that routes live market events into the session.
- **Gateway**: The FastAPI WebSocket handler in `app/deriv_gateway.py` (`/deriv/ws` and `/deriv/live/market`) that manages all Deriv communication.
- **contract_normalization**: The canonical settlement normalization module at `app/contract_normalization.py`.
- **Unknown Outcome**: A contract whose terminal status cannot be determined because the Deriv response timed out, was rejected, or was otherwise unresolvable.
- **Red-alone Qualification**: The approved behavior in `evaluate_color_positions` where a unique Red digit on the correct side is sufficient to pass the colour-position filter, even when Yellow falls outside the qualifying zone.
- **Window Size**: The number of observed ticks used to calculate digit-frequency percentages; valid values are defined per requirement.
- **Percentage Trigger**: An increase of exactly 0.1 percentage point in a monitored digit's frequency, which arms the tick-sequence tracker.
- **Settlement**: The terminal resolution of a Deriv contract, providing a confirmed status and profit.

---

## Requirements

### Requirement 1: CI Fix Failing Tests in test_digit_strategy.py

**User Story:** As a developer, I want all existing tests in `tests/test_digit_strategy.py` to pass with correct fixtures and correct assertions, so that CI is green and the test suite reliably exercises the intended behaviors.

#### Acceptance Criteria

1. WHEN `test_over3_yellow_at_9_percent_fails_when_yellow_is_in_zone` runs, THE Test Suite SHALL use a fixture whose digit-0 and digit-1 percentages are distinct (no Red tie) so the test reaches the Yellow-at-9% check and asserts `False` because Yellow is ≥ 9%.
2. WHEN `test_over5_red_alone_is_not_invalidated_by_yellow_outside_zone` runs, THE Test Suite SHALL use a fixture where exactly one Red digit lies in Over 5's low-side zone (digits 0–6), Green and Blue both lie in the high-side zone (digits 7–9), and Yellow lies outside the low-side zone; the assertion SHALL be `True` because Red-alone qualification passes.
3. WHEN `test_under6_red_alone_qualifies_when_yellow_is_outside_zone` runs, THE Test Suite SHALL use a fixture where exactly one Red digit lies in Under 6's high-side zone (digits 5–9), Green and Blue both lie in the low-side zone (digits 0–4), and Yellow lies outside the high-side zone (i.e., Yellow is in digits 0–4); the assertion SHALL be `True` because Red-alone qualification passes.
4. THE Test Suite SHALL not modify any production code in `app/strategy/digit_strategy.py` to make these three tests pass; only the test fixtures and assertions are changed.
5. WHEN all tests in `tests/test_digit_strategy.py` are executed, THE Test Suite SHALL report zero failures and zero errors.

---

### Requirement 2: CI Add New Tests for Corrected Behaviors

**User Story:** As a developer, I want new targeted tests for the behaviors the repaired fixtures now verify, so that Red-alone qualification and the Yellow-in-zone failure are independently proven by dedicated test cases.

#### Acceptance Criteria

1. WHEN Yellow is in the Over-B qualifying zone and its percentage is exactly 9.0%, THE Test Suite SHALL include a test asserting that `evaluate_color_positions` returns `False` for `Group.OVER`.
2. WHEN Yellow is in the Over-B qualifying zone and its percentage is 8.9%, THE Test Suite SHALL include a test asserting that `evaluate_color_positions` returns `True` for `Group.OVER`.
3. WHEN Yellow is outside the Over-B qualifying zone and Red is unique and below 9%, THE Test Suite SHALL include a test asserting that `evaluate_color_positions` returns `True` (Red-alone qualification).
4. WHEN Yellow is outside the Under-B qualifying zone and Red is unique and below 9%, THE Test Suite SHALL include a test asserting that `evaluate_color_positions` returns `True` (Red-alone qualification).
5. THE Test Suite SHALL execute these new tests without modifying any production code.

---

### Requirement 3: Active-Run Lifecycle Reject New Start During Unresolved Execution

**User Story:** As a backend engineer, I want the Gateway to block a new strategy `start` action while the current execution has a pending proposal, a pending buy, or an open contract, so that two separate execution sequences cannot overlap.

#### Acceptance Criteria

1. WHEN a strategy `start` action is received and `StrategyExecution` is not `None` and its state is `WAITING_PROPOSAL`, `WAITING_BUY`, or `WAITING_SETTLEMENT`, THE Gateway SHALL send an error response with code `STRATEGY_ALREADY_ACTIVE` and not create a new `StrategyExecution` or `LiveStrategyFeed`.
2. WHEN a strategy `start` action is received and `StrategyExecution` state is `IDLE`, `COMPLETED`, or `STOPPED`, THE Gateway SHALL allow the start and create a fresh `StrategyExecution` and reset the `LiveStrategyFeed`.
3. WHEN a strategy `start` action is received and `StrategyExecution` state is `BLOCKED`, THE Gateway SHALL require that the blocked state include a reconciled or cleared contract before allowing a fresh start, and SHALL send `STRATEGY_BLOCKED_RECONCILIATION_REQUIRED` if an unreconciled contract ID is still present.
4. THE Gateway SHALL include the current execution state in the `STRATEGY_ALREADY_ACTIVE` error response body so the client can display it.

---

### Requirement 4: Active-Run Lifecycle Prevent Duplicate Tick Subscriptions

**User Story:** As a backend engineer, I want the Gateway to track active tick subscriptions per symbol and skip re-subscribing when a strategy is started again on the same symbol, so that Deriv does not receive duplicate subscription requests.

#### Acceptance Criteria

1. WHEN a strategy `start` action is received for a symbol that already has an active upstream `ticks` subscription, THE Gateway SHALL not send a second `{"ticks": symbol, "subscribe": 1}` message to Deriv.
2. WHEN a strategy `start` action is received for a symbol that does not have an active upstream subscription, THE Gateway SHALL send exactly one `{"ticks": symbol, "subscribe": 1}` message to Deriv.
3. WHEN a strategy `stop` action or a WebSocket disconnect causes the session to end, THE Gateway SHALL record that the subscription for the associated symbol is no longer needed and SHALL send a `forget` or `forget_all` request to Deriv before closing if subscriptions are active.
4. THE Gateway SHALL track subscribed symbols in a local in-memory set scoped to the individual WebSocket connection lifecycle.

---

### Requirement 5: Active-Run Lifecycle Preserve Active Contract Through stop()

**User Story:** As a backend engineer, I want `StrategyExecution.stop()` to preserve the active contract ID when a contract is open, so that reconciliation logic has the information it needs after a forced stop.

#### Acceptance Criteria

1. WHEN `StrategyExecution.stop()` is called while `active_contract_id` is not `None`, THE `StrategyExecution` SHALL retain `active_contract_id` in its state and set `state` to `STOPPED` without clearing `active_contract_id`.
2. WHEN `StrategyExecution.stop()` is called while `active_contract_id` is `None`, THE `StrategyExecution` SHALL set state to `STOPPED` and leave `active_contract_id` as `None`.
3. WHEN `StrategyExecution.status()` is called after a stop with a preserved contract ID, THE `StrategyExecution` SHALL include `active_contract_id` in the returned status dict so the caller can initiate reconciliation.
4. THE `StrategyExecution` unit tests SHALL cover both stop-with-active-contract and stop-without-active-contract paths.

---

### Requirement 6: Safe Order Reconciliation Treat Buy Timeout as Unknown Not Failure

**User Story:** As a backend engineer, I want a timed-out buy or proposal to be recorded as an unknown outcome rather than a failure, so that the system never retries or continues without knowing whether a contract was created.

#### Acceptance Criteria

1. WHEN the strategy response timeout monitor detects an expired `pending_strategy_proposals` or `pending_strategy_buys` entry, THE Gateway SHALL transition `StrategyExecution` to `BLOCKED` state with reason `UNKNOWN_OUTCOME` and SHALL NOT treat the timeout as a loss or decrement consecutive-loss tracking.
2. WHEN `StrategyExecution` enters `BLOCKED` with reason `UNKNOWN_OUTCOME`, THE Gateway SHALL send a status message to the client with `state: "blocked"`, `reason: "unknown_outcome"`, and the last known `active_contract_id` (if any) so the client can display a reconciliation prompt.
3. WHEN `StrategyExecution` is in `BLOCKED` with `reason: "unknown_outcome"`, THE Gateway SHALL not accept a new strategy `start` action until the user has explicitly confirmed reconciliation or a reconciliation query returns a confirmed terminal outcome.
4. IF a `proposal_open_contract` settlement message arrives for a contract ID that matches the preserved `active_contract_id` after a timeout block, THEN THE Gateway SHALL resume settlement processing using the confirmed outcome and clear the `UNKNOWN_OUTCOME` block state.

---

### Requirement 7: Safe Order Reconciliation Settlement Deadline Monitoring After Buy

**User Story:** As a backend engineer, I want the Gateway to enforce a settlement deadline after a buy acknowledgement, so that a contract that never settles does not leave the execution sequence permanently stuck in `WAITING_SETTLEMENT`.

#### Acceptance Criteria

1. WHEN `StrategyExecution.on_buy()` confirms a contract ID and transitions to `WAITING_SETTLEMENT`, THE Gateway SHALL register a settlement deadline for that contract ID using the configured `TRADEBRIDGE_STRATEGY_RESPONSE_TIMEOUT_SECONDS` value (default 15 seconds).
2. WHEN the settlement deadline expires before a `proposal_open_contract` with a terminal status is received, THE Gateway SHALL transition `StrategyExecution` to `BLOCKED` with reason `UNKNOWN_OUTCOME`, preserve `active_contract_id`, and notify the client.
3. WHEN a `proposal_open_contract` message arrives with a terminal status before the deadline, THE Gateway SHALL cancel the settlement deadline and process the settlement normally.
4. THE Gateway SHALL use the same deadline registry (`strategy_request_deadlines`) for settlement deadlines as it already uses for proposal and buy deadlines.

---

### Requirement 8: Safe Order Reconciliation Portfolio Query for Unknown Outcomes

**User Story:** As a backend engineer, I want the Gateway to query the Deriv portfolio or transaction history when a contract outcome is unknown, so that the system can confirm whether the contract was created and settled before allowing another run.

#### Acceptance Criteria

1. WHEN a client sends a `{"strategy": {"action": "reconcile", "contract_id": "<id>"}}` message and `StrategyExecution` is in `BLOCKED` state with a matching `active_contract_id`, THE Gateway SHALL send a `{"portfolio": 1}` or `{"profit_table": 1, "contract_id": "<id>"}` request to Deriv to look up the contract.
2. WHEN the Deriv portfolio response confirms a terminal status for the reconciled contract ID, THE Gateway SHALL update `StrategyExecution` using the confirmed outcome and transition to `COMPLETED` or `STOPPED` as appropriate, then send the updated status to the client.
3. WHEN the Deriv portfolio response does not contain the contract ID or returns an error, THE Gateway SHALL keep `StrategyExecution` in `BLOCKED` state, notify the client with a `RECONCILIATION_FAILED` message, and not allow a new strategy start.
4. THE Gateway SHALL never automatically retry a contract buy when the outcome is unknown; the user must trigger reconciliation explicitly.

---

### Requirement 9: Strategy Spec Resolution Document Red-Alone Qualification

**User Story:** As a developer, I want `docs/strategy/STRATEGY_SOURCE_OF_TRUTH.md` to explicitly state that Red-alone qualification is intentional, so that future reviewers do not mistake the code behavior for a bug.

#### Acceptance Criteria

1. THE `STRATEGY_SOURCE_OF_TRUTH.md` file SHALL contain a clarification note in the colour-positions section stating that when Yellow falls outside the selected direction's qualifying zone, Red-alone qualification passes and Yellow's position does not invalidate the setup.
2. THE clarification note SHALL reference the barrier-relative zone boundaries for both Over and Under directions.
3. THE production code in `app/strategy/digit_strategy.py` `evaluate_color_positions` SHALL remain unchanged by this requirement.
4. WHEN `STRATEGY_SOURCE_OF_TRUTH.md` is reviewed, THE document SHALL contain no statements that contradict Red-alone qualification being an approved and deliberate rule.

---

### Requirement 10: Strategy Spec Resolution Clarify Approved Barrier Coverage

**User Story:** As a developer, I want the strategy documentation to clearly state which barriers are covered by generalized rules versus which remain observation-only, so that the code's `strategy_profile_supported` function and the documentation are consistent.

#### Acceptance Criteria

1. THE `STRATEGY_SOURCE_OF_TRUTH.md` file SHALL state that the generalized barrier formulas apply to any barrier for which both the activity band and the confirmation band are non-empty, not only to Over 3 and Under 6.
2. THE document SHALL retain Over 3 and Under 6 as the primary validated examples while acknowledging that other barriers (e.g., Over 4, Under 5) are covered by the same generalized rules.
3. THE document SHALL state that barriers producing an empty confirmation band (e.g., Over 7, Over 8, Under 2) are observation-only regardless of other qualification conditions.
4. THE production code in `app/strategy/digit_strategy.py` `strategy_profile_supported` SHALL remain the authoritative gatekeeper and SHALL not be modified by this requirement.

---

### Requirement 11: Window Size Restriction Enforce 1000-Tick Window Only

**User Story:** As a backend engineer, I want the Gateway and `LiveStrategyFeed` to accept only a 1000-tick window size for strategy starts, so that all qualification logic, percentage calculations, and tests operate on the single approved window.

#### Acceptance Criteria

1. WHEN a strategy `start` action is received with `window_size` equal to any value other than `1000`, THE Gateway SHALL reject the request immediately with error code `INVALID_STRATEGY_WINDOW` and message `"Strategy window_size must be 1000 ticks."`.
2. WHEN a strategy `start` action is received with `window_size` equal to `1000`, THE Gateway SHALL proceed with the start normally.
3. THE `LiveStrategyFeed.start()` method SHALL raise `ValueError` with message `"Strategy window_size must be 1000 ticks."` for any `window_size` other than `1000`.
4. THE existing `PercentageFeedHealth` validation and session logic SHALL not be affected by this restriction; they operate on the window after it has been accepted.
5. WHEN all tests in `tests/test_live_strategy_feed.py`, `tests/test_strategy_session.py`, and `tests/test_strategy_execution.py` are executed after this change, THE Test Suite SHALL pass with all `window_size` references updated to `1000`.

---

### Requirement 12: Window Size Restriction Update Tests to Use 1000-Tick Window

**User Story:** As a developer, I want all strategy tests that pass a `window_size` to use `1000`, so that the tests remain valid after the gateway restriction is enforced.

#### Acceptance Criteria

1. THE `tests/test_live_strategy_feed.py` file SHALL not contain any call to `LiveStrategyFeed.start()` or the Gateway strategy start action with a `window_size` other than `1000`.
2. WHEN any test constructs a strategy frequency snapshot for a window other than `1000`, THE Test Suite SHALL update that fixture to use the `"1000"` window key.
3. IF a test previously asserted that `window_size` values of `50`, `100`, `250`, or `500` were accepted, THEN THE Test Suite SHALL update that assertion to verify that those values are now rejected with `INVALID_STRATEGY_WINDOW`.
4. THE Test Suite SHALL add at least one test confirming that `window_size: 1000` is the only accepted value in the Gateway validation path.

---

### Requirement 13: Centralized Settlement Normalization Wire contract_normalization

**User Story:** As a backend engineer, I want all Deriv contract settlement processing in `deriv_gateway.py` to route through `contract_normalization.normalize_settlement()`, so that settlement handling is consistent, auditable, and free of ad-hoc status-string comparisons.

#### Acceptance Criteria

1. WHEN a `proposal_open_contract` message is received with a terminal contract update, THE Gateway SHALL pass the contract dict and the execution stake to `contract_normalization.normalize_settlement()` and use the returned `outcome`, `profit`, and `payout` fields to drive `StrategyExecution.on_settlement()`.
2. WHEN `normalize_settlement()` returns `None` for a contract update (non-terminal or ambiguous), THE Gateway SHALL not call `StrategyExecution.on_settlement()` and SHALL not advance the execution state.
3. WHEN `normalize_settlement()` returns a result with `outcome == "won"`, THE Gateway SHALL pass `status="won"` and the confirmed `profit` to `StrategyExecution.on_settlement()`.
4. WHEN `normalize_settlement()` returns a result with `outcome == "lost"`, THE Gateway SHALL pass `status="lost"` and the confirmed `profit` to `StrategyExecution.on_settlement()`.
5. WHEN `normalize_settlement()` returns a result with `outcome == "breakeven"` or `outcome == "cancelled"`, THE Gateway SHALL treat the outcome as non-loss (consecutive-loss count not incremented) and pass this to `on_settlement()`.
6. THE inline ad-hoc settlement logic in `deriv_gateway.py` (`is_sold`, direct `status` string comparisons for won/lost) SHALL be replaced by the call to `normalize_settlement()`; no duplicate settlement-parsing logic SHALL remain.

---

### Requirement 14: Centralized Settlement Normalization Halt on Ambiguous Terminal Status

**User Story:** As a backend engineer, I want the Gateway to block execution when `normalize_settlement()` indicates an ambiguous or unrecognized terminal state, so that the system never silently miscounts a loss or win.

#### Acceptance Criteria

1. IF a `proposal_open_contract` message passes `is_terminal_contract()` but `normalize_settlement()` returns `None`, THEN THE Gateway SHALL transition `StrategyExecution` to `BLOCKED` with reason `AMBIGUOUS_SETTLEMENT` and preserve `active_contract_id`.
2. WHEN `StrategyExecution` enters `BLOCKED` with reason `AMBIGUOUS_SETTLEMENT`, THE Gateway SHALL send a status message to the client identifying the contract ID and the ambiguous provider fields.
3. THE Gateway SHALL not submit the next sequential buy until a non-`None` settlement result from `normalize_settlement()` has been processed for the current contract.
4. THE `tests/test_contract_normalization.py` test file SHALL cover the cases: lost with no explicit profit, won with explicit profit, sold with sell_price, contradictory lost+positive profit returning None, and non-terminal update returning None.
