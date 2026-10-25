# Implementation Plan: AI-TradeBridge Audit Remediation

## Overview

Remediate six categories of defect identified during the digit-strategy audit. Tasks follow the priority sequence: CI green first, then active-run lifecycle, safe order reconciliation, spec documentation, window-size restriction, and centralized settlement normalization. All changes target Python 3.11+, FastAPI/WebSocket, and demo accounts only.

## Tasks

- [x] 1. Fix three failing test fixtures in `tests/test_digit_strategy.py`
  - [x] 1.1 Fix `test_over3_yellow_at_9_percent_fails_when_yellow_is_in_zone`
    - In `tests/test_digit_strategy.py`, update the fixture in `AutomaticSelectionTests` so digit-0 is the sole Red (8.0%) and digit-1 is Yellow in-zone at exactly 9.0%, with no Red tie; all digits 5–9 occupy the high-side zone for Over 3
    - Keep `self.assertFalse(ok)` and `self.assertIn("strictly below 9%", reason)` — the test must fail for the correct reason (Yellow ≥ 9%), not a Red tie
    - Do not modify any production code in `app/strategy/digit_strategy.py`
    - _Requirements: 1.1, 1.4_

  - [x] 1.2 Fix `test_over5_red_alone_is_not_invalidated_by_yellow_outside_zone`
    - Replace the fixture with: `{0:7.8, 1:8.5, 2:8.6, 3:8.7, 4:8.8, 5:9.0, 6:9.1, 7:10.2, 8:8.0, 9:10.5}` — digit-0 is Red (7.8%), digit-8 is Yellow (8.0%, outside zone 0–6), digit-9 is Green, digit-7 is Blue
    - Change the assertion from `self.assertFalse(ok)` to `self.assertTrue(ok)` and add `self.assertIn("Red-alone qualification", reason)`
    - Do not modify any production code
    - _Requirements: 1.2, 1.4_

  - [x] 1.3 Fix `test_under6_red_alone_qualifies_when_yellow_is_outside_zone`
    - Keep the existing fixture `{0:10.9, 1:10.8, 2:10.7, 3:10.6, 4:8.1, 5:8.0, 6:8.2, 7:8.8, 8:9.3, 9:9.4}` (digit-5 is Red, digit-4 is Yellow outside zone 5–9)
    - Change the assertion from `self.assertFalse(ok)` to `self.assertTrue(ok)` and add `self.assertIn("Red-alone qualification", reason)`
    - Do not modify any production code
    - _Requirements: 1.3, 1.4_

  - [ ]* 1.4 Run `tests/test_digit_strategy.py` and confirm zero failures
    - Execute `python -m pytest tests/test_digit_strategy.py -v` against the virtualenv and confirm all tests pass
    - _Requirements: 1.5_

- [x] 2. Add new targeted tests for Red-alone qualification behaviors
  - [x] 2.1 Add `test_yellow_at_exactly_9_percent_in_over_zone_fails` to `AutomaticSelectionTests`
    - Fixture: `{0:8.0, 1:9.0, 2:8.5, 3:9.1, 4:9.2, 5:10.0, 6:10.1, 7:10.2, 8:10.8, 9:10.9}`, assert `evaluate_color_positions(p, Group.OVER, barrier=3)` returns `False`
    - _Requirements: 2.1_

  - [x] 2.2 Add `test_yellow_at_8_point_9_in_over_zone_passes` to `AutomaticSelectionTests`
    - Fixture: digit-1 at 8.9% (below 9%); assert `evaluate_color_positions` returns `True`
    - _Requirements: 2.2_

  - [x] 2.3 Add `test_over_red_alone_qualifies_when_yellow_outside_zone` to `AutomaticSelectionTests`
    - Fixture: `{0:7.8, 1:8.5, 2:8.6, 3:8.7, 4:8.8, 5:9.0, 6:9.1, 7:10.2, 8:8.0, 9:10.5}`, assert `evaluate_color_positions(p, Group.OVER, barrier=5)` returns `True`
    - _Requirements: 2.3_

  - [x] 2.4 Add `test_under_red_alone_qualifies_when_yellow_outside_zone` to `AutomaticSelectionTests`
    - Fixture: `{0:10.9, 1:10.8, 2:10.7, 3:10.6, 4:8.1, 5:8.0, 6:8.2, 7:8.8, 8:9.3, 9:9.4}`, assert `evaluate_color_positions(p, Group.UNDER, barrier=6)` returns `True`
    - _Requirements: 2.4_

  - [ ]* 2.5 Write property test for Red-alone qualification (Over direction)
    - **Property 1: Red-Alone Qualification is Universal for Over Direction**
    - Use `hypothesis` to generate percentage maps where: unique Red in `0..B+1`, Green and Blue both in `B+2..9`, Red percentage < 9%, Yellow falls outside `0..B+1`; assert `evaluate_color_positions` returns `True` for all conforming inputs
    - **Validates: Requirements 2.3**

  - [ ]* 2.6 Write property test for Red-alone qualification (Under direction)
    - **Property 2: Red-Alone Qualification is Universal for Under Direction**
    - Use `hypothesis` to generate percentage maps satisfying Under B structural requirements with Yellow outside `B-1..9`; assert `evaluate_color_positions` returns `True`
    - **Validates: Requirements 2.4**

- [x] 3. Checkpoint — Ensure all tests pass, ask the user if questions arise.

- [ ] 4. Add `block_reason` to `StrategyExecution` and fix `stop()` contract-ID preservation
  - [ ] 4.1 Add `block_reason: str | None = None` field to `StrategyExecution` dataclass in `app/strategy/execution.py`
    - Add `block_reason` to the `status()` return dict alongside the existing fields
    - _Requirements: 6.1, 6.2, 14.1_

  - [ ] 4.2 Update `block()` and `_block()` to accept and store an optional `block_reason` parameter
    - Change signature: `def block(self, reason: str, block_reason: str | None = None) -> None`
    - In `_block()`: set `self.block_reason = block_reason`; clear `active_contract_id` only when `block_reason` is NOT `"UNKNOWN_OUTCOME"` or `"AMBIGUOUS_SETTLEMENT"`; always clear `pending_proposal_request` and `pending_buy_request`
    - _Requirements: 6.1, 14.1_

  - [ ] 4.3 Fix `stop()` to preserve `active_contract_id` when it is set
    - Remove the `self.active_contract_id = None` line from `stop()`; the method must leave `active_contract_id` unchanged, setting it to `None` only if it was already `None`
    - _Requirements: 5.1, 5.2_

  - [ ]* 4.4 Write unit tests for `stop()` with and without active contract
    - Add tests to `tests/test_strategy_execution.py`: `test_stop_preserves_active_contract_id_when_set` and `test_stop_leaves_active_contract_id_none_when_already_none`
    - _Requirements: 5.4_

  - [ ]* 4.5 Write property test for `stop()` contract-ID preservation
    - **Property 5: stop() Preserves Contract ID When Set**
    - Use `hypothesis` to generate `StrategyExecution` instances in `WAITING_SETTLEMENT` state with arbitrary non-`None` `active_contract_id` strings; assert `stop()` results in `state == STOPPED` and `active_contract_id` unchanged
    - **Validates: Requirements 5.1**

- [ ] 5. Implement gateway start-action guard in `/deriv/ws`
  - [ ] 5.1 Add active-state guard before creating a new `StrategyExecution` in `client_to_deriv()` in `app/deriv_gateway.py`
    - If `strategy_execution.state` is in `{WAITING_PROPOSAL, WAITING_BUY, WAITING_SETTLEMENT}`, send `{"error": {"code": "STRATEGY_ALREADY_ACTIVE", "message": "A strategy execution sequence is already active.", "execution_state": <state.value>}}` and `continue`
    - _Requirements: 3.1, 3.4_

  - [ ] 5.2 Add reconciliation-required guard for BLOCKED/STOPPED states with unresolved contract
    - If `strategy_execution.state` is in `{BLOCKED, STOPPED}` and `strategy_execution.active_contract_id is not None`, send `{"error": {"code": "STRATEGY_BLOCKED_RECONCILIATION_REQUIRED", "message": "...", "execution_state": <state.value>, "active_contract_id": <id>}}` and `continue`
    - _Requirements: 3.3_

  - [ ] 5.3 Allow fresh start when state is `IDLE`, `COMPLETED`, `STOPPED` (no contract), or `BLOCKED` (no contract)
    - Existing start flow proceeds for these states; no additional guard needed beyond 5.1–5.2
    - _Requirements: 3.2_

  - [ ]* 5.4 Write unit/integration tests for gateway start-action guard
    - Add tests to `tests/test_deriv_gateway.py` (or equivalent) verifying each guard condition: `WAITING_PROPOSAL` → `STRATEGY_ALREADY_ACTIVE`; `BLOCKED` with contract → `STRATEGY_BLOCKED_RECONCILIATION_REQUIRED`; `COMPLETED` → start proceeds
    - _Requirements: 3.1, 3.2, 3.3_

- [ ] 6. Implement subscription deduplication in `/deriv/ws`
  - [ ] 6.1 Add `subscribed_symbols: set[str] = set()` to connection-scoped variables in `deriv_websocket_proxy()` in `app/deriv_gateway.py`
    - _Requirements: 4.4_

  - [ ] 6.2 Guard the `await upstream.send({"ticks": symbol, "subscribe": 1})` call in the `start` action handler
    - Only send the subscribe message when `symbol not in subscribed_symbols`; add `subscribed_symbols.add(symbol)` unconditionally after strategy setup succeeds
    - _Requirements: 4.1, 4.2_

  - [ ] 6.3 Add `forget_all` send on `stop` action when `subscribed_symbols` is non-empty
    - In the `stop` action branch: `if subscribed_symbols: await upstream.send(json.dumps({"forget_all": "ticks"}, ...)); subscribed_symbols.clear()`
    - _Requirements: 4.3_

  - [ ]* 6.4 Write unit tests for subscription deduplication
    - Verify second `start` on same symbol sends no extra subscribe message; verify `stop` sends `forget_all` when subscriptions are active; verify first `start` sends exactly one subscribe message
    - _Requirements: 4.1, 4.2, 4.3_

- [ ] 7. Checkpoint — Ensure all tests pass, ask the user if questions arise.

- [ ] 8. Implement timeout → BLOCKED with `UNKNOWN_OUTCOME` reason
  - [ ] 8.1 Update `strategy_response_timeout_monitor()` in `app/deriv_gateway.py` to call `block()` with `block_reason="UNKNOWN_OUTCOME"`
    - Change `strategy_execution.block(f"Timed out waiting for Deriv {phase} response.")` to `strategy_execution.block(f"Timed out waiting for Deriv {phase} response.", block_reason="UNKNOWN_OUTCOME")`
    - Update the `send_json` status message to include `"Outcome is unknown; reconciliation required before continuing."`
    - _Requirements: 6.1, 6.2_

  - [ ]* 8.2 Write property test for timeout → BLOCKED never loss
    - **Property 6: Timeout Always Produces BLOCKED Never Loss**
    - Use `hypothesis` to generate `StrategyExecution` in `WAITING_PROPOSAL` or `WAITING_BUY` states; simulate deadline expiry; assert `state == BLOCKED`, `block_reason == "UNKNOWN_OUTCOME"`, and `consecutive_losses` is unchanged
    - **Validates: Requirements 6.1**

- [ ] 9. Implement settlement deadline monitoring after buy acknowledgement
  - [ ] 9.1 Add `pending_settlement_deadline_id: int | None = None` to connection-scoped variables in `deriv_websocket_proxy()` in `app/deriv_gateway.py`
    - _Requirements: 7.4_

  - [ ] 9.2 Register a settlement deadline after `strategy_execution.on_buy()` returns a subscribe message
    - After `await upstream.send(json.dumps(subscribe_message, ...))`, create a settlement deadline: `settlement_deadline_id = next_strategy_request_id(); strategy_request_deadlines[settlement_deadline_id] = ("settlement", time.monotonic() + float(os.getenv("TRADEBRIDGE_STRATEGY_RESPONSE_TIMEOUT_SECONDS", "15"))); pending_settlement_deadline_id = settlement_deadline_id`
    - _Requirements: 7.1, 7.4_

  - [ ] 9.3 Cancel the settlement deadline when a terminal `proposal_open_contract` arrives before expiry
    - In the `proposal_open_contract` handler, before calling `strategy_execution.on_settlement()`, pop `pending_settlement_deadline_id` from `strategy_request_deadlines` and set it to `None`
    - _Requirements: 7.3_

  - [ ] 9.4 Confirm existing `strategy_response_timeout_monitor()` handles `phase == "settlement"` correctly
    - The monitor already pops expired entries and calls `block()` with `UNKNOWN_OUTCOME`; confirm `active_contract_id` is preserved (from Task 4.2) and no additional changes are needed for the expiry path
    - _Requirements: 7.2_

- [ ] 10. Implement reconcile action handler and portfolio query
  - [ ] 10.1 Add `pending_reconcile_requests: dict[int, str] = {}` to connection-scoped variables in `deriv_websocket_proxy()` in `app/deriv_gateway.py`
    - _Requirements: 8.1_

  - [ ] 10.2 Add `"reconcile"` to the supported strategy actions list and implement the handler in `client_to_deriv()`
    - Parse `contract_id = control.get("contract_id")` from the message
    - Validate `strategy_execution is not None`, `state is BLOCKED`, and `str(contract_id).strip() == strategy_execution.active_contract_id`; send appropriate error responses for each failure case (`NO_EXECUTION`, `NOT_BLOCKED`, `CONTRACT_ID_MISMATCH`)
    - On success, generate `reconcile_request_id = next_strategy_request_id()`, store in `pending_reconcile_requests`, and `await upstream.send({"profit_table": 1, "description": 1, "req_id": reconcile_request_id})`
    - _Requirements: 8.1, 8.4_

  - [ ] 10.3 Handle `profit_table` responses in `deriv_to_client()` for reconcile requests
    - Check `req_id in pending_reconcile_requests`; pop the entry and search `transactions` for the matching `contract_id`
    - If not found, send `RECONCILIATION_FAILED`; if found, call `normalize_settlement(match, stake=strategy_execution.stake)` and either send `RECONCILIATION_FAILED` (None result) or call `strategy_execution.on_settlement()` and resume the execution flow
    - Update the supported actions error message to include `"reconcile"`
    - _Requirements: 8.2, 8.3_

  - [ ]* 10.4 Write unit tests for reconcile action handler
    - Test: matching contract found and normalized → execution resumes; contract not in profit_table → `RECONCILIATION_FAILED`; `normalize_settlement` returns None → `RECONCILIATION_FAILED`; contract_id mismatch → `CONTRACT_ID_MISMATCH`; called when not blocked → `NOT_BLOCKED`
    - _Requirements: 8.1, 8.2, 8.3_

- [ ] 11. Checkpoint — Ensure all tests pass, ask the user if questions arise.

- [ ] 12. Restrict `window_size` to 1000 only
  - [ ] 12.1 Update `LiveStrategyFeed.start()` in `app/strategy/live_feed.py`
    - Change: `if type(window_size) is not int or window_size not in (50, 100, 250, 500, 1000):` → `if type(window_size) is not int or window_size != 1000:`
    - Change error message to: `"Strategy window_size must be 1000 ticks."`
    - _Requirements: 11.3_

  - [ ] 12.2 Update the `/deriv/ws` start action handler in `app/deriv_gateway.py`
    - Change the `window_size` guard to `window_size != 1000` and update the error message to `"Strategy window_size must be 1000 ticks."`
    - _Requirements: 11.1, 11.2_

  - [ ] 12.3 Update the `/deriv/live/market` start action handler in `app/deriv_gateway.py`
    - Same change as 12.2 for the `client_to_upstream()` function's `window_size` guard
    - _Requirements: 11.1, 11.2_

- [ ] 13. Update tests to use `window_size=1000`
  - [ ] 13.1 Update `tests/test_live_strategy_feed.py` to replace all non-1000 `window_size` values
    - Replace `window_size=100` with `window_size=1000` in `test_strategy_uses_the_selected_digit_window_for_snapshots`
    - Replace `window_size=250` with `window_size=1000` in `test_start_uses_the_selected_barrier_and_fifteen_second_window`
    - Update `test_strategy_rejects_unsupported_selected_digit_window` error message assertion from `"one of 50, 100, 250, 500, or 1000"` to `"1000 ticks"`
    - _Requirements: 12.1, 12.3_

  - [ ] 13.2 Add `test_window_size_1000_is_the_only_accepted_value` to `tests/test_live_strategy_feed.py`
    - Assert `window_size=1000` is accepted (no `ValueError`); assert `window_size=500` raises `ValueError` with `"1000 ticks"` in the message
    - _Requirements: 12.4_

  - [ ]* 13.3 Write property test for window size restriction at `LiveStrategyFeed`
    - **Property 3: Window Size Restriction is Universally Enforced at LiveStrategyFeed**
    - Use `hypothesis` with `integers()` filtered to exclude 1000; assert every non-1000 integer raises `ValueError` containing `"1000 ticks"`; assert 1000 does not raise
    - **Validates: Requirements 11.3**

  - [ ]* 13.4 Write property test for window size restriction at the gateway
    - **Property 4: Window Size Restriction is Universally Enforced at the Gateway**
    - Use `hypothesis` with any non-1000 `window_size` value in a strategy start message; assert the gateway returns `code == "INVALID_STRATEGY_WINDOW"` and does not create an execution
    - **Validates: Requirements 11.1**

- [ ] 14. Wire `contract_normalization.normalize_settlement()` into gateway settlement handler
  - [ ] 14.1 Add imports for `is_terminal_contract` and `normalize_settlement` from `app.contract_normalization` at the top of `app/deriv_gateway.py` (or inline at point of use)
    - _Requirements: 13.1, 13.6_

  - [ ] 14.2 Replace the inline settlement detection block in `deriv_to_client()` in `/deriv/ws`
    - Remove: `settled = contract.get("is_sold") in (1, True, "1") or contract_status in {"won", "lost", "sold"}`
    - Replace with: `if is_terminal_contract(contract):`; then `settlement_result = normalize_settlement(contract, stake=strategy_execution.stake)`
    - If `settlement_result is None`, call `strategy_execution.block("Settlement normalization returned None for a terminal contract.", block_reason="AMBIGUOUS_SETTLEMENT")`, send the blocked status to the client including the contract ID, send `strategy_feed.unlock_execution()`, and do not call `on_settlement()`
    - If `settlement_result` is not None, call `strategy_execution.on_settlement(str(contract_id), settlement_result["outcome"], settlement_result["profit"], next_request_id)` and handle the resulting `STOPPED`, `COMPLETED`, or next-proposal flow as before
    - Cancel `pending_settlement_deadline_id` before processing (from Task 9.3)
    - _Requirements: 13.1, 13.2, 13.3, 13.4, 13.5, 13.6, 14.1_

  - [ ]* 14.3 Write unit tests for ambiguous settlement → BLOCKED
    - **Property 8: Ambiguous Settlement Always Produces BLOCKED Never Incorrect Outcome**
    - Use `hypothesis` to generate terminal contract dicts where `is_terminal_contract()` is True but `normalize_settlement()` returns None (e.g., lost + positive profit); assert `state == BLOCKED`, `block_reason == "AMBIGUOUS_SETTLEMENT"`, and `consecutive_losses` unchanged
    - **Validates: Requirements 14.1**

  - [ ]* 14.4 Verify and expand `tests/test_contract_normalization.py` coverage
    - Confirm (or add) tests for: lost with no explicit profit → profit is `-stake`; won with explicit profit → returned as-is; sold with `sell_price` → payout is `sell_price`; contradictory lost + positive profit → returns `None`; non-terminal update → returns `None`
    - _Requirements: 14.4_

  - [ ]* 14.5 Write property test for settlement normalization usage guarantee
    - **Property 7: Settlement Normalization is Always Used for Terminal Contracts**
    - For any `proposal_open_contract` message for which `is_terminal_contract()` returns True, verify the gateway path only calls `on_settlement()` when `normalize_settlement()` returns non-None
    - **Validates: Requirements 13.1, 13.2**

- [ ] 15. Update `docs/strategy/STRATEGY_SOURCE_OF_TRUTH.md`
  - [ ] 15.1 Add Red-alone qualification clarification note to the colour-positions subsections of Over B and Under B
    - Insert the note: "**Red-alone qualification:** When Yellow falls outside the selected direction's qualifying zone, the colour-position check passes on the strength of Red alone. Yellow being outside the zone does not invalidate the setup. This is an intentional and approved rule."
    - For Over B: add that if Yellow is outside `0..B+1` (i.e., in `B+2..9`), Red-alone qualification applies; Green and Blue must still occupy `B+2..9`
    - For Under B: add that if Yellow is outside `B-1..9` (i.e., in `0..B-2`), Red-alone qualification applies; Green and Blue must still occupy `0..B-2`
    - _Requirements: 9.1, 9.2, 9.4_

  - [ ] 15.2 Update the **Generalized barrier formulas** introduction to clarify approved barrier coverage
    - Add a paragraph stating the formulas apply to any barrier B for which both the activity band and the confirmation band are non-empty (not only Over 3 and Under 6)
    - Explicitly acknowledge Over 4 and Under 5 as also covered
    - State that barriers with empty confirmation bands (e.g., Over 7, Over 8, Under 2) are observation-only regardless of other qualification conditions, and that `strategy_profile_supported()` in `app/strategy/digit_strategy.py` is the authoritative gatekeeper
    - Update the `window_size` reference in step 2 of the Run lifecycle from `"50, 100, 250, 500, or 1,000 observed ticks"` to `"1,000 observed ticks"` to stay consistent with Requirement 11
    - _Requirements: 10.1, 10.2, 10.3, 10.4_

- [ ] 16. Final checkpoint — Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP delivery
- Tasks 1–3 (CI fix group) must be completed before any production code changes are introduced
- Tasks 4–4.3 are prerequisites for Tasks 5, 8, and 9 because the typed `block_reason` and preserved `active_contract_id` are dependencies
- Task 12 (window restriction) is safe to apply before Task 13 (test updates) only if the tests are updated in the same commit; apply them together to keep CI green
- Task 14 (normalize_settlement wiring) must come after Task 4 (block_reason) because the `AMBIGUOUS_SETTLEMENT` path requires `block_reason` support
- Property tests require `hypothesis` to be installed; add it to `requirements.txt` if not present
- No real-account execution changes; all gateway guards remain demo-only
- Each task references specific requirements for traceability

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "1.2", "1.3"] },
    { "id": 1, "tasks": ["1.4", "2.1", "2.2", "2.3", "2.4"] },
    { "id": 2, "tasks": ["2.5", "2.6", "4.1"] },
    { "id": 3, "tasks": ["4.2", "4.3"] },
    { "id": 4, "tasks": ["4.4", "4.5", "5.1", "5.2", "5.3"] },
    { "id": 5, "tasks": ["5.4", "6.1", "6.2", "6.3"] },
    { "id": 6, "tasks": ["6.4", "8.1", "9.1"] },
    { "id": 7, "tasks": ["8.2", "9.2", "10.1"] },
    { "id": 8, "tasks": ["9.3", "9.4", "10.2"] },
    { "id": 9, "tasks": ["10.3", "12.1", "12.2", "12.3"] },
    { "id": 10, "tasks": ["10.4", "13.1", "13.2"] },
    { "id": 11, "tasks": ["13.3", "13.4", "14.1"] },
    { "id": 12, "tasks": ["14.2", "15.1", "15.2"] },
    { "id": 13, "tasks": ["14.3", "14.4", "14.5"] }
  ]
}
```
