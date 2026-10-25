# Design Document

## AI-TradeBridge Audit Remediation

## Overview

This document covers the full technical design for the six categories of defect identified during the digit-strategy audit. The work is sequenced by priority:

1. **CI Fix** — repair three broken test fixtures in `tests/test_digit_strategy.py` without changing production code.
2. **Active-Run Lifecycle** — gateway start-action guard, duplicate-subscription prevention, and `stop()` contract-ID preservation.
3. **Safe Order Reconciliation** — timeout → BLOCKED transition, settlement deadline monitoring, reconcile action handler, and portfolio query.
4. **Strategy Spec Resolution** — documentation updates to `STRATEGY_SOURCE_OF_TRUTH.md`.
5. **Window Size Restriction** — lock `window_size` to 1000 everywhere.
6. **Centralized Settlement Normalization** — wire `contract_normalization.normalize_settlement()` into the gateway settlement handler.

All components are Python 3.11+, FastAPI/WebSocket, with state held in-memory per WebSocket connection. Demo accounts only; real-account execution remains disabled.

---

## Architecture

The system's layered architecture is unchanged. This remediation adds defensive guards and state-machine transitions at the gateway and execution layers without restructuring the existing modules.

```
Browser / Client
       │  WebSocket  │  WebSocket
       ▼             ▼
  /deriv/ws     /deriv/live/market
  (authenticated) (public observation)
       │
  LiveStrategyFeed (per connection)
       │
  DigitStrategySession (state machine, side-effect-free)
       │
  DigitFrequencyWindow / PercentageFeedHealth
       │
  StrategyExecution (demo-only sequential buy coordinator)
       │
  contract_normalization.normalize_settlement()  ←  WIRED IN
```

The two significant new state-machine additions are:

- `StrategyExecution.BLOCKED` with typed reasons (`UNKNOWN_OUTCOME`, `AMBIGUOUS_SETTLEMENT`)
- Gateway-level subscription tracking (`_subscribed_symbols: set[str]` per connection)

---

## Components and Interfaces

### 1. Test Fixture Corrections

**No production code changes.** Only fixtures and assertions in three existing tests are corrected.

#### 1.1 `test_over3_yellow_at_9_percent_fails_when_yellow_is_in_zone`

**Problem:** The current fixture (`p.update({1: 8.0, 2: 9.0, 3: 9.1, 4: 9.2})`) leaves digit-0 at `8.0` and digit-1 at `8.0`, creating a Red tie that causes `evaluate_color_positions` to fail at the Red-count check before it ever reaches the Yellow-at-9% check. The test therefore fails for the wrong reason.

**Fix:** Use a fixture where digit-0 is the sole Red (strictly lowest), digit-1 is Yellow (second lowest, in-zone, at 9.0%), all other conditions are satisfied for Over 3.

```python
def test_over3_yellow_at_9_percent_fails_when_yellow_is_in_zone(self):
    p = over3_percentages()
    # digit-0 is sole Red (8.0%); digit-1 is Yellow in-zone at exactly 9.0%.
    # Green/Blue must be in digits 5–9 (Over 3 high-side: B+2..9 = 5..9).
    p.update({0: 8.0, 1: 9.0, 2: 8.5, 3: 9.1,
              4: 9.2, 5: 10.0, 6: 10.1, 7: 10.2, 8: 10.8, 9: 10.9})
    ok, reason = evaluate_color_positions(p, Group.OVER, barrier=3)
    self.assertFalse(ok)
    self.assertIn("strictly below 9%", reason)
```

The default `over3_percentages()` already has digit-0 as Red. What needs adjusting is ensuring digit-1 is strictly the second-lowest (Yellow at 9.0%) without tying digit-0. The fixture above achieves this: digit-0 = 8.0 (Red), digit-1 = 9.0 (Yellow, in-zone, at the boundary).

#### 1.2 `test_over5_red_alone_is_not_invalidated_by_yellow_outside_zone`

**Problem:** The current fixture has the current assertion `self.assertFalse(ok)` with a comment that Green/Blue ranks do not satisfy high-side requirements. This means the test is asserting failure, but the requirement is to test *Red-alone qualification passes*. The fixture needs to be constructed so that it does satisfy all structural requirements except Yellow is outside the qualifying zone.

**Over 5 zones:**
- Red/Yellow qualifying zone: digits 0..6 (B+1 = 6, so zone is 0..B+1 = 0..6)
- Green/Blue zone: digits 7..9 (B+2..9 = 7..9)
- Red must be unique and in 0..6
- Green and Blue must be in 7..9
- Yellow must be outside 0..6 (i.e., in 7..9)

**Fix:**

```python
def test_over5_red_alone_is_not_invalidated_by_yellow_outside_zone(self):
    # Over 5: low-side qualifying zone is 0..6; high-side is 7..9.
    # Red = digit 0 (sole minimum, in-zone, < 9%).
    # Yellow = digit 8 (second lowest overall, but outside the 0..6 zone).
    # Green = digit 9 (highest). Blue = digit 7 (second highest).
    # All threshold digits (0..5) must be below 10.0%; one may reach up to 10.5%.
    p = {
        0: 7.8, 1: 8.5, 2: 8.6, 3: 8.7, 4: 8.8, 5: 9.0,
        6: 9.1, 7: 10.2, 8: 8.0, 9: 10.5,
    }
    # digit-0 is Red (lowest=7.8%), digit-8 is Yellow (second lowest=8.0%, outside 0..6 zone)
    # digit-9 is Green (highest=10.5%), digit-7 is Blue (second highest=10.2%)
    ok, reason = evaluate_color_positions(p, Group.OVER, barrier=5)
    self.assertTrue(ok)
    self.assertIn("Red-alone qualification", reason)
```

Verify this fixture before finalizing:
- Levels sorted: 7.8, 8.0, 8.5, 8.6, 8.7, 8.8, 9.0, 9.1, 10.2, 10.5
- Red = {0} (7.8%), Yellow = {8} (8.0%), Blue = {7} (10.2%), Green = {9} (10.5%)
- Red {0} ⊆ 0..6 ✓; Green {9} ⊆ 7..9 ✓; Blue {7} ⊆ 7..9 ✓
- Yellow {8} ⊄ 0..6 → Yellow is outside the low-side zone → Red-alone path
- Red percentage 7.8% < 9% ✓
- Over 5 threshold digits: 0..5; values 7.8, 8.5, 8.6, 8.7, 8.8, 9.0 — all 6 below 10.0% ✓
- assertion: `assertTrue(ok)` ✓

#### 1.3 `test_under6_red_alone_qualifies_when_yellow_is_outside_zone`

**Problem:** The current fixture ends with `p = {0:10.9, 1:10.8, 2:10.7, 3:10.6, 4:8.1, 5:8.0, 6:8.2, 7:8.8, 8:9.3, 9:9.4}`. In this fixture, digit-5 is Red (8.0%). Under 6's high-side Red/Yellow zone is digits 5..9. Digit-5 is in the zone, but digit-4 (8.1%) is the second-lowest, making it Yellow at 8.1% and Yellow is *outside* the zone (digit 4 < 5). However, the test asserts `self.assertFalse(ok)` with the comment "Red=5 is outside Under 6's Red zone." — that comment is wrong: digit-5 IS in the Under 6 red zone (5..9). The assertion is incorrect.

**Under 6 zones:**
- Red/Yellow qualifying zone: digits 5..9 (B-1..9 = 5..9)
- Green/Blue zone: digits 0..4 (0..B-2 = 0..4)
- Red must be unique and in 5..9
- Green and Blue must be in 0..4
- Yellow is in 0..4 (outside high-side zone) → Red-alone qualification

**Fix:** Keep the fixture but change the assertion to `assertTrue(ok)`:

```python
def test_under6_red_alone_qualifies_when_yellow_is_outside_zone(self):
    # Under 6: high-side qualifying zone is 5..9; low-side is 0..4.
    # Red = digit 5 (lowest = 8.0%, in-zone).
    # Yellow = digit 4 (second lowest = 8.1%, outside high-side zone → Red-alone).
    # Green = digit 0 (highest = 10.9%). Blue = digit 1 (second highest = 10.8%).
    p = {0: 10.9, 1: 10.8, 2: 10.7, 3: 10.6, 4: 8.1,
         5: 8.0, 6: 8.2, 7: 8.8, 8: 9.3, 9: 9.4}
    ok, reason = evaluate_color_positions(p, Group.UNDER, barrier=6)
    self.assertTrue(ok)
    self.assertIn("Red-alone qualification", reason)
```

Verify:
- Levels sorted: 8.0, 8.1, 8.2, 8.8, 9.3, 9.4, 10.6, 10.7, 10.8, 10.9
- Red = {5} (8.0%), Yellow = {4} (8.1%), Blue = {1} (10.8%), Green = {0} (10.9%)
- Red {5} ⊆ 5..9 ✓; Green {0} ⊆ 0..4 ✓; Blue {1} ⊆ 0..4 ✓
- Yellow {4} ⊄ 5..9 → Yellow is outside the high-side zone → Red-alone path
- Red percentage 8.0% < 9% ✓
- Under 6 threshold digits: 6..9; values 8.2, 8.8, 9.3, 9.4 — all 4 below 10.0% ✓
- assertion: `assertTrue(ok)` ✓

#### 1.4 New Tests for Requirement 2

Four new targeted test methods to add to `AutomaticSelectionTests`:

```python
def test_yellow_at_exactly_9_percent_in_over_zone_fails(self):
    """Yellow in-zone at 9.0% must fail (strict boundary)."""
    p = {0: 8.0, 1: 9.0, 2: 8.5, 3: 9.1,
         4: 9.2, 5: 10.0, 6: 10.1, 7: 10.2, 8: 10.8, 9: 10.9}
    ok, _ = evaluate_color_positions(p, Group.OVER, barrier=3)
    self.assertFalse(ok)

def test_yellow_at_8_point_9_in_over_zone_passes(self):
    """Yellow in-zone at 8.9% must pass (just below strict boundary)."""
    p = {0: 8.0, 1: 8.9, 2: 8.5, 3: 9.1,
         4: 9.2, 5: 10.0, 6: 10.1, 7: 10.2, 8: 10.8, 9: 10.9}
    ok, _ = evaluate_color_positions(p, Group.OVER, barrier=3)
    self.assertTrue(ok)

def test_over_red_alone_qualifies_when_yellow_outside_zone(self):
    """Red-alone qualification passes for Over when Yellow is outside the low-side zone."""
    p = {0: 7.8, 1: 8.5, 2: 8.6, 3: 8.7, 4: 8.8, 5: 9.0,
         6: 9.1, 7: 10.2, 8: 8.0, 9: 10.5}
    ok, _ = evaluate_color_positions(p, Group.OVER, barrier=5)
    self.assertTrue(ok)

def test_under_red_alone_qualifies_when_yellow_outside_zone(self):
    """Red-alone qualification passes for Under when Yellow is outside the high-side zone."""
    p = {0: 10.9, 1: 10.8, 2: 10.7, 3: 10.6, 4: 8.1,
         5: 8.0, 6: 8.2, 7: 8.8, 8: 9.3, 9: 9.4}
    ok, _ = evaluate_color_positions(p, Group.UNDER, barrier=6)
    self.assertTrue(ok)
```

---

### 2. StrategyExecution State Machine Extensions (`app/strategy/execution.py`)

#### 2.1 BLOCKED State with Typed Reasons

Add a `block_reason` attribute to `StrategyExecution` to record why a BLOCKED transition occurred. The two new reasons are `UNKNOWN_OUTCOME` and `AMBIGUOUS_SETTLEMENT`.

```python
block_reason: str | None = None  # added field
```

Extend `block()` and `_block()` to accept an optional reason string and store it:

```python
def block(self, reason: str, block_reason: str | None = None) -> None:
    self._block(reason, block_reason=block_reason)

def _block(self, reason: str, block_reason: str | None = None) -> None:
    self.state = ExecutionState.BLOCKED
    self.block_reason = block_reason
    self.pending_proposal_request = None
    self.pending_buy_request = None
    # NOTE: active_contract_id is NOT cleared here — preservation is intentional
    # for UNKNOWN_OUTCOME and AMBIGUOUS_SETTLEMENT paths (Requirements 5, 6, 7, 14).
```

The current `_block()` clears `active_contract_id`. This must change: clearing `active_contract_id` on every BLOCKED transition defeats the reconciliation design. The fix is to clear it only when the BLOCKED reason does not require it to be preserved:

```python
def _block(self, reason: str, block_reason: str | None = None) -> None:
    self.state = ExecutionState.BLOCKED
    self.block_reason = block_reason
    self.pending_proposal_request = None
    self.pending_buy_request = None
    if block_reason not in ("UNKNOWN_OUTCOME", "AMBIGUOUS_SETTLEMENT"):
        self.active_contract_id = None
```

Update `status()` to include `block_reason`:

```python
def status(self, reason: str) -> dict[str, Any]:
    return {
        "type": "strategy_execution_status",
        "state": self.state.value,
        "reason": reason,
        "block_reason": self.block_reason,
        "symbol": self.symbol,
        "direction": self.group.value,
        "barrier": self.barrier,
        "buy_count": self.buy_count,
        "max_buys": self.max_buys,
        "consecutive_losses": self.consecutive_losses,
        "active_contract_id": self.active_contract_id,
    }
```

#### 2.2 `stop()` Contract-ID Preservation

The current `stop()` unconditionally sets `active_contract_id = None`. Change it to preserve the contract ID when it is set:

```python
def stop(self) -> None:
    self.state = ExecutionState.STOPPED
    self.pending_proposal_request = None
    self.pending_buy_request = None
    # active_contract_id is preserved if it is not None so that the caller
    # can initiate reconciliation after a forced stop.
    if self.active_contract_id is None:
        pass  # already None; nothing to preserve
    # Do not set self.active_contract_id = None here.
```

This satisfies Requirements 5.1 and 5.2: if the contract ID is set, it survives the stop; if it is already `None`, no change is needed.

#### 2.3 Guard: Reject New Start During Unresolved Execution

The gateway must check `StrategyExecution.state` before allowing a new `start` action. The decision table is:

| `strategy_execution.state` | `active_contract_id` | Gateway Action |
|---|---|---|
| `None` (no execution yet) | — | Allow start |
| `IDLE` | — | Allow start |
| `COMPLETED` | — | Allow start |
| `STOPPED` | `None` | Allow start |
| `STOPPED` | non-`None` | Send `STRATEGY_BLOCKED_RECONCILIATION_REQUIRED` |
| `BLOCKED` | `None` | Allow start (cleared BLOCKED) |
| `BLOCKED` | non-`None` | Send `STRATEGY_BLOCKED_RECONCILIATION_REQUIRED` |
| `WAITING_PROPOSAL` | — | Send `STRATEGY_ALREADY_ACTIVE` |
| `WAITING_BUY` | — | Send `STRATEGY_ALREADY_ACTIVE` |
| `WAITING_SETTLEMENT` | — | Send `STRATEGY_ALREADY_ACTIVE` |

Gateway pseudo-code (inside `client_to_deriv` / `client_to_upstream`):

```python
if action == "start":
    if strategy_execution is not None:
        active_states = {
            ExecutionState.WAITING_PROPOSAL,
            ExecutionState.WAITING_BUY,
            ExecutionState.WAITING_SETTLEMENT,
        }
        reconcile_required_states = {ExecutionState.BLOCKED, ExecutionState.STOPPED}
        if strategy_execution.state in active_states:
            await websocket.send_json({
                "error": {
                    "code": "STRATEGY_ALREADY_ACTIVE",
                    "message": "A strategy execution sequence is already active.",
                    "execution_state": strategy_execution.state.value,
                }
            })
            continue
        if (
            strategy_execution.state in reconcile_required_states
            and strategy_execution.active_contract_id is not None
        ):
            await websocket.send_json({
                "error": {
                    "code": "STRATEGY_BLOCKED_RECONCILIATION_REQUIRED",
                    "message": "A previous contract outcome is unresolved; reconcile before starting again.",
                    "execution_state": strategy_execution.state.value,
                    "active_contract_id": strategy_execution.active_contract_id,
                }
            })
            continue
    # ... proceed with start
```

---

### 3. Subscription Lifecycle Management (`app/deriv_gateway.py`)

#### 3.1 In-Memory Subscription Set

Both WebSocket handlers (`/deriv/ws` and `/deriv/live/market`) already maintain an `strategy_subscribed_symbol` variable or `subscribed_symbols` set. The `/deriv/live/market` handler already has `subscribed_symbols: set[str]` and a guard `if not already_subscribed`. The `/deriv/ws` handler only tracks `strategy_subscribed_symbol` as a single variable and always sends the subscribe message.

**`/deriv/live/market`:** Already correct — the `already_subscribed` guard at line `already_subscribed = symbol in subscribed_symbols` prevents duplicate subscription messages. No change needed for 4.1–4.2.

**`/deriv/ws`:** Needs a `_subscribed_symbols: set[str]` (local to the connection) and a guard before sending `{"ticks": symbol, "subscribe": 1}`:

```python
# At connection scope:
subscribed_symbols: set[str] = set()

# In the start action handler, before sending the ticks subscribe:
if symbol not in subscribed_symbols:
    await upstream.send(json.dumps({"ticks": symbol, "subscribe": 1}, separators=(",", ":")))
    subscribed_symbols.add(symbol)
```

#### 3.2 Subscription Cleanup on Stop/Disconnect

When the strategy session ends (stop action or WebSocket disconnect), send a `forget_all` for ticks to Deriv if any subscriptions are active:

```python
# In the stop action handler:
if subscribed_symbols:
    await upstream.send(json.dumps({"forget_all": "ticks"}, separators=(",", ":")))
    subscribed_symbols.clear()
```

For disconnect (the `except WebSocketDisconnect` / `except websockets.exceptions.ConnectionClosed` block), the upstream connection is already closing, so no additional forget message is needed — the upstream WebSocket close automatically terminates subscriptions.

---

### 4. Timeout Handling: BLOCKED Not Loss

#### 4.1 Proposal and Buy Timeouts

The existing `strategy_response_timeout_monitor()` already calls `strategy_execution.block(...)`. The change is to pass the typed reason and preserve `active_contract_id`:

```python
async def strategy_response_timeout_monitor() -> None:
    while True:
        await asyncio.sleep(0.25)
        if strategy_execution is None:
            continue
        now = time.monotonic()
        expired = [
            request_id for request_id, (_, deadline) in strategy_request_deadlines.items()
            if deadline <= now
        ]
        for request_id in expired:
            phase, _ = strategy_request_deadlines.pop(request_id)
            pending_strategy_proposals.discard(request_id)
            pending_strategy_buys.discard(request_id)
            # Preserve active_contract_id by using block_reason="UNKNOWN_OUTCOME"
            strategy_execution.block(
                f"Timed out waiting for Deriv {phase} response.",
                block_reason="UNKNOWN_OUTCOME",
            )
            await websocket.send_json(
                strategy_execution.status(
                    f"Automatic execution blocked: Deriv {phase} response timed out. "
                    "Outcome is unknown; reconciliation required before continuing."
                )
            )
            await websocket.send_json(strategy_feed.unlock_execution())
```

Note: `strategy_execution.block()` with `block_reason="UNKNOWN_OUTCOME"` must NOT clear `active_contract_id`. For a buy timeout, `active_contract_id` may not yet be set (the buy has not been acknowledged), in which case it stays `None`. For a settlement timeout (see 4.2), it was set during `on_buy()`.

#### 4.2 Settlement Deadline Monitoring

After `strategy_execution.on_buy()` confirms a contract ID and transitions to `WAITING_SETTLEMENT`, the gateway must register a settlement deadline using the same `strategy_request_deadlines` dictionary:

```python
elif provider_message.get("msg_type") == "buy":
    subscribe_message = strategy_execution.on_buy(request_id, provider_message)
    if subscribe_message is None:
        await websocket.send_json(
            strategy_execution.status("Strategy execution blocked: invalid buy response.")
        )
        await websocket.send_json(strategy_feed.unlock_execution())
    else:
        # Register a settlement deadline using the contract_id as the key surrogate.
        # Use a synthetic request_id for the deadline entry.
        settlement_deadline_id = next_strategy_request_id()
        strategy_request_deadlines[settlement_deadline_id] = (
            "settlement",
            time.monotonic() + float(os.getenv("TRADEBRIDGE_STRATEGY_RESPONSE_TIMEOUT_SECONDS", "15")),
        )
        # Store it so we can cancel it when settlement arrives.
        pending_settlement_deadline_id = settlement_deadline_id
        await upstream.send(json.dumps(subscribe_message, separators=(",", ":")))
        await websocket.send_json(
            strategy_execution.status("Buy accepted; waiting for contract settlement before continuing.")
        )
```

When `proposal_open_contract` with a terminal status arrives before the deadline:

```python
if settled and isinstance(contract_id, (str, int)):
    # Cancel the settlement deadline if one is registered.
    strategy_request_deadlines.pop(pending_settlement_deadline_id, None)
    pending_settlement_deadline_id = None
    # ... proceed to normalize_settlement and on_settlement
```

`pending_settlement_deadline_id` is a connection-scoped variable initialized to `None`.

---

### 5. Reconcile Action Handler

Add a new strategy action `"reconcile"` to both WebSocket handlers. This action is only meaningful in `/deriv/ws` where `StrategyExecution` exists.

#### 5.1 Client Message Format

```json
{"strategy": {"action": "reconcile", "contract_id": "<id>"}}
```

#### 5.2 Handler Logic

```python
elif action == "reconcile":
    contract_id = control.get("contract_id")
    if strategy_execution is None:
        await websocket.send_json({
            "error": {"code": "NO_EXECUTION", "message": "No strategy execution is active."}
        })
        continue
    if strategy_execution.state is not ExecutionState.BLOCKED:
        await websocket.send_json({
            "error": {"code": "NOT_BLOCKED", "message": "Reconciliation is only available when execution is blocked."}
        })
        continue
    if (
        not isinstance(contract_id, (str, int))
        or str(contract_id).strip() != (strategy_execution.active_contract_id or "")
    ):
        await websocket.send_json({
            "error": {
                "code": "CONTRACT_ID_MISMATCH",
                "message": "Contract ID does not match the blocked execution's contract.",
            }
        })
        continue
    # Issue a profit_table query scoped to this contract_id for closed contracts.
    # Also check portfolio for potentially open contracts.
    reconcile_request_id = next_strategy_request_id()
    pending_reconcile_requests[reconcile_request_id] = str(contract_id)
    await upstream.send(json.dumps({
        "profit_table": 1,
        "description": 1,
        "req_id": reconcile_request_id,
    }, separators=(",", ":")))
    await websocket.send_json({
        "type": "strategy_reconcile_status",
        "contract_id": str(contract_id),
        "message": "Reconciliation query sent to Deriv.",
    })
```

`pending_reconcile_requests: dict[int, str]` maps request ID to the contract ID being reconciled.

#### 5.3 Reconcile Response Handler

In `deriv_to_client()`, handle `profit_table` responses:

```python
if provider_message.get("msg_type") == "profit_table":
    req_id = provider_message.get("req_id")
    if isinstance(req_id, int) and req_id in pending_reconcile_requests:
        contract_id = pending_reconcile_requests.pop(req_id)
        transactions = (provider_message.get("profit_table") or {}).get("transactions") or []
        match = next(
            (t for t in transactions if str(t.get("contract_id", "")) == contract_id),
            None,
        )
        if match is None:
            await websocket.send_json({
                "type": "strategy_reconcile_status",
                "contract_id": contract_id,
                "code": "RECONCILIATION_FAILED",
                "message": "Contract not found in Deriv transaction history.",
            })
        else:
            # Normalize the matched transaction using contract_normalization.
            from app.contract_normalization import normalize_settlement
            settlement_result = normalize_settlement(match, stake=strategy_execution.stake)
            if settlement_result is None:
                await websocket.send_json({
                    "type": "strategy_reconcile_status",
                    "contract_id": contract_id,
                    "code": "RECONCILIATION_FAILED",
                    "message": "Contract found but settlement cannot be normalized.",
                })
            else:
                next_req = next_strategy_request_id()
                next_proposal = strategy_execution.on_settlement(
                    contract_id,
                    settlement_result["outcome"],
                    settlement_result["profit"],
                    next_req,
                )
                if next_proposal is not None:
                    pending_strategy_proposals.add(next_req)
                    register_strategy_deadline(next_req, "proposal")
                    await upstream.send(json.dumps(next_proposal, separators=(",", ":")))
                await websocket.send_json(
                    strategy_execution.status("Reconciliation complete; execution resumed.")
                )
```

---

### 6. Window Size Restriction

#### 6.1 `LiveStrategyFeed.start()`

Change the validation from accepting `(50, 100, 250, 500, 1000)` to accepting only `1000`:

```python
# Before:
if type(window_size) is not int or window_size not in (50, 100, 250, 500, 1000):
    raise ValueError("Strategy window_size must be one of 50, 100, 250, 500, or 1000 ticks.")

# After:
if type(window_size) is not int or window_size != 1000:
    raise ValueError("Strategy window_size must be 1000 ticks.")
```

#### 6.2 Gateway `/deriv/ws`

```python
# Before:
if type(window_size) is not int or window_size not in (50, 100, 250, 500, 1000):
    await websocket.send_json({"error": {"code": "INVALID_STRATEGY_WINDOW", "message": "Strategy window_size must be 50, 100, 250, 500, or 1000 ticks."}})

# After:
if type(window_size) is not int or window_size != 1000:
    await websocket.send_json({"error": {"code": "INVALID_STRATEGY_WINDOW", "message": "Strategy window_size must be 1000 ticks."}})
```

#### 6.3 Gateway `/deriv/live/market`

Same change as 6.2:

```python
if type(window_size) is not int or window_size != 1000:
    await websocket.send_json({"error": {"code": "INVALID_STRATEGY_WINDOW", "message": "Strategy window_size must be 1000 ticks."}})
```

#### 6.4 Test Updates

In `tests/test_live_strategy_feed.py`:

- `test_strategy_uses_the_selected_digit_window_for_snapshots`: Remove the call with `window_size=100`; replace with `window_size=1000`.
- `test_start_uses_the_selected_barrier_and_fifteen_second_window`: Replace `window_size=250` with `window_size=1000`.
- `test_strategy_rejects_unsupported_selected_digit_window`: Keep but update the error message assertion from `"one of 50, 100, 250, 500, or 1000"` to `"1000 ticks"`.
- Add: `test_window_size_1000_is_the_only_accepted_value` that asserts `window_size=1000` is accepted and `window_size=500` is rejected.

In `tests/test_strategy_session.py`: No `window_size` is passed directly; no changes needed.

In `tests/test_strategy_execution.py`: No `window_size` references; no changes needed.

---

### 7. Centralized Settlement Normalization

#### 7.1 Import

`normalize_settlement` and `is_terminal_contract` are already importable from `app.contract_normalization`. Add the import to `app/deriv_gateway.py`:

```python
from app.contract_normalization import is_terminal_contract, normalize_settlement
```

#### 7.2 Replace Inline Settlement Logic

The current inline check in `deriv_to_client()` uses ad-hoc string comparisons:

```python
# Current (to be removed):
settled = contract.get("is_sold") in (1, True, "1") or contract_status in {"won", "lost", "sold"}
```

Replace with:

```python
from app.contract_normalization import is_terminal_contract, normalize_settlement

# In deriv_to_client(), where proposal_open_contract is handled:
if strategy_execution is not None and provider_message.get("msg_type") == "proposal_open_contract":
    contract = provider_message.get("proposal_open_contract")
    if isinstance(contract, dict):
        contract_id = contract.get("contract_id")
        if is_terminal_contract(contract) and isinstance(contract_id, (str, int)):
            # Cancel settlement deadline if registered.
            if pending_settlement_deadline_id is not None:
                strategy_request_deadlines.pop(pending_settlement_deadline_id, None)
                pending_settlement_deadline_id = None

            settlement_result = normalize_settlement(contract, stake=strategy_execution.stake)

            if settlement_result is None:
                # Terminal flags present but outcome is ambiguous (e.g., lost + positive profit).
                strategy_execution.block(
                    "Settlement normalization returned None for a terminal contract.",
                    block_reason="AMBIGUOUS_SETTLEMENT",
                )
                await websocket.send_json(
                    strategy_execution.status(
                        f"Execution blocked: ambiguous settlement for contract {contract_id}. "
                        "Provider fields may be contradictory."
                    )
                )
                await websocket.send_json(strategy_feed.unlock_execution())
            else:
                outcome = settlement_result["outcome"]
                profit = settlement_result["profit"]
                next_request_id = next_strategy_request_id()
                next_proposal = strategy_execution.on_settlement(
                    str(contract_id),
                    outcome,
                    profit,
                    next_request_id,
                )
                if next_proposal is not None:
                    pending_strategy_proposals.add(next_request_id)
                    register_strategy_deadline(next_request_id, "proposal")
                    await upstream.send(json.dumps(next_proposal, separators=(",", ":")))
                    await websocket.send_json(
                        strategy_execution.status("Contract settled; requesting the next sequential buy.")
                    )
                elif strategy_execution.state is ExecutionState.STOPPED:
                    await websocket.send_json(
                        strategy_execution.status("Two consecutive losses reached; no further buys will be submitted.")
                    )
                    await websocket.send_json(strategy_feed.unlock_execution())
                    await websocket.send_json(strategy_feed.stop())
                elif strategy_execution.state is ExecutionState.COMPLETED:
                    await websocket.send_json(
                        strategy_execution.status("Three sequential buys completed; strategy execution stopped.")
                    )
                    await websocket.send_json(strategy_feed.unlock_execution())
                    await websocket.send_json(strategy_feed.stop())
```

#### 7.3 `on_settlement()` Outcome Mapping

`normalize_settlement()` returns `outcome` as `"won"`, `"lost"`, `"breakeven"`, or `"cancelled"`. The current `StrategyExecution.on_settlement()` calls `_outcome()` internally, which re-parses the status string. After the normalization refactor, `on_settlement()` receives the already-resolved outcome. The `_outcome()` helper can be bypassed:

```python
def on_settlement(
    self,
    contract_id: str,
    status: str | None,
    profit: Any,
    next_request_id: int,
) -> dict[str, Any] | None:
    if self.state is not ExecutionState.WAITING_SETTLEMENT or contract_id != self.active_contract_id:
        return None
    # status now arrives as normalize_settlement()'s "outcome" field:
    # "won", "lost", "breakeven", "cancelled"
    is_loss = status in ("lost",) or (
        status not in ("won", "breakeven", "cancelled")
        and self._outcome(status, profit) == "loss"
    )
    ...
```

However, to minimize changes to `on_settlement()` internals, the gateway can pass the normalized outcome as the `status` parameter and rely on the existing `_outcome()` logic. The key guarantee from `normalize_settlement()` is:
- `outcome == "won"` → `profit > 0`
- `outcome == "lost"` → `profit < 0`
- `outcome == "breakeven"` → `profit == 0`
- `outcome == "cancelled"` → `profit == 0`

The existing `_outcome()` will correctly classify these since `profit` is now always a confirmed finite number. No changes to `on_settlement()` or `_outcome()` are strictly required — the gateway simply passes `settlement_result["outcome"]` as `status` and `settlement_result["profit"]` as `profit`.

---

### 8. Documentation Updates (`docs/strategy/STRATEGY_SOURCE_OF_TRUTH.md`)

#### 8.1 Red-Alone Qualification Clarification

Add the following note to the colour-positions subsections within **Generalized barrier formulas**:

> **Red-alone qualification:** When Yellow falls outside the selected direction's qualifying zone, the colour-position check passes on the strength of Red alone. Yellow being outside the zone does not invalidate the setup. This is an intentional and approved rule.
>
> - **Over B:** Red must be within digits `0..B+1`. If Yellow is outside `0..B+1` (i.e., Yellow falls in `B+2..9`), Red-alone qualification applies and Yellow's position has no effect on the result. Green and Blue must still occupy `B+2..9`.
> - **Under B:** Red must be within digits `B-1..9`. If Yellow is outside `B-1..9` (i.e., Yellow falls in `0..B-2`), Red-alone qualification applies and Yellow's position has no effect on the result. Green and Blue must still occupy `0..B-2`.

#### 8.2 Approved Barrier Coverage Clarification

Update the **Generalized barrier formulas** introduction to explicitly state the coverage scope:

> The formulas below apply to **any** barrier `B` for which both the activity band and the confirmation band are non-empty. Over 3 and Under 6 remain the primary validated examples, but other barriers — including Over 4, Under 5, and others — are covered by the same generalized rules.
>
> Barriers that produce an empty confirmation band (for example, Over 7: `B+3..9 = 10..9` is empty; Over 8: `B+3..9 = 11..9` is empty; Under 2: `0..B-3 = 0..-1` is empty) are **observation-only** regardless of all other qualification conditions. The function `strategy_profile_supported()` in `app/strategy/digit_strategy.py` is the authoritative gatekeeper for this rule and must not be modified.

---

## Data Models

#### 9.1 `StrategyExecution` (modified fields)

| Field | Type | Default | Notes |
|---|---|---|---|
| `state` | `ExecutionState` | `IDLE` | Existing; `BLOCKED` is an existing enum value |
| `block_reason` | `str \| None` | `None` | **New** — `"UNKNOWN_OUTCOME"` or `"AMBIGUOUS_SETTLEMENT"` |
| `active_contract_id` | `str \| None` | `None` | Existing; preserved on BLOCKED with typed reason |

#### 9.2 `StrategyExecution.status()` response (modified)

```json
{
  "type": "strategy_execution_status",
  "state": "blocked",
  "reason": "...",
  "block_reason": "UNKNOWN_OUTCOME",
  "symbol": "1HZ100V",
  "direction": "over",
  "barrier": 3,
  "buy_count": 1,
  "max_buys": 3,
  "consecutive_losses": 0,
  "active_contract_id": "1234567890"
}
```

#### 9.3 Gateway connection-scoped variables (added)

| Variable | Type | Scope | Purpose |
|---|---|---|---|
| `subscribed_symbols` | `set[str]` | `/deriv/ws` connection | Track active tick subscriptions |
| `pending_settlement_deadline_id` | `int \| None` | `/deriv/ws` connection | Cancel settlement deadline on normal settlement |
| `pending_reconcile_requests` | `dict[int, str]` | `/deriv/ws` connection | Map reconcile request IDs to contract IDs |

---

## Error Handling

| Scenario | Gateway Action | `StrategyExecution` State |
|---|---|---|
| Start received during active execution | Send `STRATEGY_ALREADY_ACTIVE` | Unchanged |
| Start received with BLOCKED+unresolved contract | Send `STRATEGY_BLOCKED_RECONCILIATION_REQUIRED` | Unchanged |
| Proposal/buy timeout | Send `strategy_execution_status` with BLOCKED | `BLOCKED` / `UNKNOWN_OUTCOME` |
| Settlement deadline timeout | Send `strategy_execution_status` with BLOCKED | `BLOCKED` / `UNKNOWN_OUTCOME` |
| Terminal contract with ambiguous normalization | Send `strategy_execution_status` with BLOCKED | `BLOCKED` / `AMBIGUOUS_SETTLEMENT` |
| Reconcile: contract not in profit_table | Send `RECONCILIATION_FAILED` | Unchanged (`BLOCKED`) |
| Reconcile: normalize_settlement returns None | Send `RECONCILIATION_FAILED` | Unchanged (`BLOCKED`) |
| Reconcile: success | Resume settlement processing | `WAITING_PROPOSAL` or `STOPPED`/`COMPLETED` |
| `window_size != 1000` at gateway | Send `INVALID_STRATEGY_WINDOW` | No execution created |
| `window_size != 1000` at LiveStrategyFeed | Raise `ValueError` | No session created |

---

## Testing Strategy

The remediation uses a dual testing approach: example-based unit tests for specific behaviors and boundary conditions, and property-based tests for universally quantified invariants.

**Unit tests (example-based):**
- Fixture corrections in `tests/test_digit_strategy.py` (Requirements 1, 2)
- `StrategyExecution.stop()` with and without active contract (Requirement 5)
- Gateway start-action guard for each execution state (Requirements 3, 6)
- `normalize_settlement()` coverage: won, lost, sold, contradictory, non-terminal (Requirement 14)
- Reconcile action: matching contract found, not found, normalization fails (Requirement 8)
- Window size: `1000` accepted, any other value rejected with `INVALID_STRATEGY_WINDOW` (Requirements 11, 12)

**Property-based tests:**
- Red-alone qualification holds for all conforming fixtures (Over and Under directions)
- `window_size != 1000` always raises `ValueError` from `LiveStrategyFeed.start()`
- `window_size != 1000` always produces `INVALID_STRATEGY_WINDOW` at the gateway
- `stop()` with any non-`None` contract ID always preserves it
- Any proposal/buy timeout always produces `BLOCKED` with `UNKNOWN_OUTCOME`, never increments `consecutive_losses`
- Any terminal contract with ambiguous normalization always produces `BLOCKED` with `AMBIGUOUS_SETTLEMENT`

**Integration tests** (manual or mock-upstream):
- Gateway blocks new start during WAITING_PROPOSAL, WAITING_BUY, WAITING_SETTLEMENT states
- Subscription deduplication: second start on same symbol sends no extra subscribe message
- Settlement deadline registration and cancellation on normal settlement
- Reconcile round-trip: blocked execution → reconcile action → portfolio response → resumed

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system — essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

### Property 1: Red-Alone Qualification is Universal for Over Direction

*For any* percentage map where Over B's structural requirements are satisfied (unique Red in `0..B+1`, Green and Blue both in `B+2..9`, Red percentage strictly below 9%), if Yellow falls outside the low-side qualifying zone `0..B+1`, then `evaluate_color_positions` returns `True`.

**Validates: Requirements 2.3**

### Property 2: Red-Alone Qualification is Universal for Under Direction

*For any* percentage map where Under B's structural requirements are satisfied (unique Red in `B-1..9`, Green and Blue both in `0..B-2`, Red percentage strictly below 9%), if Yellow falls outside the high-side qualifying zone `B-1..9`, then `evaluate_color_positions` returns `True`.

**Validates: Requirements 2.4**

### Property 3: Window Size Restriction is Universally Enforced at LiveStrategyFeed

*For any* integer value other than `1000` passed as `window_size` to `LiveStrategyFeed.start()`, the call raises `ValueError` with a message containing `"1000 ticks"`. For `window_size = 1000`, no `ValueError` is raised.

**Validates: Requirements 11.3**

### Property 4: Window Size Restriction is Universally Enforced at the Gateway

*For any* strategy start action message with `window_size` not equal to `1000`, the gateway sends an error response with `code == "INVALID_STRATEGY_WINDOW"` and does not create a new execution or session.

**Validates: Requirements 11.1**

### Property 5: stop() Preserves Contract ID When Set

*For any* `StrategyExecution` instance in `WAITING_SETTLEMENT` state with a non-`None` `active_contract_id`, calling `stop()` results in `state == STOPPED` and `active_contract_id` unchanged.

**Validates: Requirements 5.1**

### Property 6: Timeout Always Produces BLOCKED Never Loss

*For any* pending proposal or buy deadline that expires, the resulting `StrategyExecution` state is `BLOCKED` with `block_reason == "UNKNOWN_OUTCOME"`, and `consecutive_losses` is unchanged from its pre-timeout value.

**Validates: Requirements 6.1**

### Property 7: Settlement Normalization is Always Used for Terminal Contracts

*For any* `proposal_open_contract` message for which `is_terminal_contract(contract)` returns `True`, the gateway uses `normalize_settlement()` to determine the outcome. The gateway never calls `on_settlement()` directly with ad-hoc status parsing, and never calls `on_settlement()` when `normalize_settlement()` returns `None`.

**Validates: Requirements 13.1, 13.2**

### Property 8: Ambiguous Settlement Always Produces BLOCKED Never Incorrect Outcome

*For any* terminal contract dict for which `is_terminal_contract()` returns `True` but `normalize_settlement()` returns `None` (contradictory fields), the resulting `StrategyExecution` state is `BLOCKED` with `block_reason == "AMBIGUOUS_SETTLEMENT"` and `consecutive_losses` is unchanged.

**Validates: Requirements 14.1**
