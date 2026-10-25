# Implementation Plan — TradeBridge Reliability Fixes

Baseline: 187 tests pass (`python -m pytest -q`). Every item must leave that true.
User constraint: no documentation changes, no README edits, no comment-only diffs.
Fix 6 (_ws_tickets comment) is **skipped entirely**.

---

## Codebase findings

### deriv_gateway.py structure

| Line range | Element |
|---|---|
| 36 | `_ws_tickets: dict[str, tuple[str, float]] = {}` |
| 612–623 | `create_websocket_ticket` endpoint |
| 625–1093 | `deriv_websocket_proxy` — the authenticated trading proxy |
| 717–762 | Variable declarations + `strategy_response_timeout_monitor` inner coroutine |
| 810–1075 | `client_to_deriv` inner coroutine |
| 923–1075 | `deriv_to_client` inner coroutine |
| 931–969 | Strategy proposal/buy response handling inside `deriv_to_client` |
| 970–999 | `proposal_open_contract` settlement detection — **Fix 2 target** |
| 1003–1060 | Tick routing — **Fix 5 target** (the `underlying_symbol` line is ~1015) |
| 1061–1075 | Manual proposal forwarding to client |
| 1079–1092 | `asyncio.wait` + teardown — **Fix 1 target (trading proxy)** |
| 1097–1293 | `live_market_proxy` — public market feed proxy |
| 1198–1278 | `upstream_to_client` inner coroutine |
| 1234 | Tick `underlying_symbol` fallback inside live proxy — **Fix 5 second instance** |
| 1279–1292 | `asyncio.wait` + teardown — **Fix 1 target (live proxy)** |

### execution.py structure

| Line range | Element |
|---|---|
| 131–157 | `on_settlement` — **Fix 4 target** |
| 184–203 | `_outcome` static method — **Fix 4 context** |
| 116–129 | `on_buy` — returns the `proposal_open_contract` subscribe message |

### Key observations

**Fix 1 — reconnect scope:** `deriv_websocket_proxy` establishes `upstream` inside one `async with websockets.connect(...)` block at line ~776. When `asyncio.wait` returns (line 1079) because either `client_to_deriv`, `deriv_to_client`, or `strategy_response_timeout_monitor` finished, the `upstream` connection is torn down by the context manager and the outer `except` at 1087 does not attempt reconnect. For the live proxy the same pattern is at lines 1117–1292. The reconnect loop must wrap the `websockets.connect` + task-spawn + `asyncio.wait` block for each proxy separately.

**Fix 2 — forget subscription ID:** Inside `deriv_to_client`, when a `proposal_open_contract` message arrives with `msg_type == "proposal_open_contract"` (line ~970), the subscription ID is in `provider_message.get("subscription", {}).get("id")` on the first message for each contract. There is currently no variable that holds this ID. A new `strategy_poc_sub_id: str | None = None` variable (alongside the existing `pending_strategy_proposals` set at line 717) needs to be introduced in the outer scope of `deriv_websocket_proxy`, then populated in `deriv_to_client` and used to send `{"forget": sub_id}` upstream before `on_settlement` is called.

**Fix 3 — credential expiry monitor:** A background coroutine (like the existing `strategy_response_timeout_monitor`) needs to be added inside `deriv_websocket_proxy`, added to the `tasks` set at line 1079, and cancelled with the rest. It needs access to `credential` (already in scope at that closure level) and `websocket`.

**Fix 4 — on_settlement fail-safe:** `on_settlement` at line 141 does `if outcome is None: return None`. The fix adds a check: if the contract is confirmed settled (the arguments already confirm it is — `on_settlement` is only called when `settled == True` in `deriv_to_client`), and `outcome` is still `None`, treat it as a loss rather than stalling. The `on_settlement` signature does NOT need to change; `is_sold`/`status` info can be inferred from the existing `status` parameter (the gateway already passes `contract_status` which will be `"won"`, `"lost"`, `"sold"`, etc. when settled). The safest approach: add a `is_confirmed_settled: bool` parameter defaulted to `False` — but that changes the signature and all callers. Instead, since `on_settlement` is ONLY ever called from `deriv_to_client` when `settled == True`, the fix can be placed at the call site in `deriv_to_client`: if `next_proposal is None` AND the execution state is still `WAITING_SETTLEMENT` (meaning `on_settlement` returned `None` due to unparseable profit), force a synthetic loss path. **Decision:** keep `on_settlement` signature unchanged; add the fail-safe in `deriv_to_client` after the `on_settlement` call, not inside `execution.py`. This avoids signature changes and keeps all callers unaffected.

**Fix 5 — underlying_symbol debug log:** The `logging` module is not imported in `deriv_gateway.py`. The functional-only change is adding a `logging.debug(...)` call when `underlying_symbol` is absent. Since importing `logging` and adding a single debug log counts as functional (it affects observable behaviour in debug mode), this is retained. One `import logging` line is added at the top.

**Fix 7 — error code granularity:** The two `strategy_execution.block(...)` calls in `deriv_to_client` for proposal error (line ~935) and buy error (line ~958) need to become switch-on-error-code logic. Retriable codes (`"RateLimit"`, `"TemporaryError"`) use `asyncio.sleep` + retry; everything else calls `block(...)` with the error code included in the reason string. The retry must be contained entirely within `deriv_to_client` since that coroutine has direct access to both `upstream` (to resend) and the strategy state sets. Max 3 retries with 1s/2s/4s backoff.

---

## Implementation order (bottom-up dependency)

Fix 4 first — it touches only `deriv_to_client` logic and has no dependencies.
Fix 2 second — introduces `strategy_poc_sub_id` variable, used by the settlement block modified in Fix 4's call site.
Fix 7 third — modifies the same proposal/buy error paths in `deriv_to_client`; after Fix 2 the settlement subscription teardown is already in place so retried buys won't double-subscribe.
Fix 5 fourth — single-line import + single debug log; entirely isolated.
Fix 3 fifth — new background coroutine added to the task set; no dependencies on 1–4.
Fix 1 last — wraps the entire connect+task loop in both proxy functions; Fix 2's `strategy_poc_sub_id` must be reset on each reconnect iteration (handled inside the loop), Fix 3's expiry-monitor task is re-spawned on each reconnect iteration naturally.

---

## Implementation Plan

- [ ] 1. **Fix 4 — `on_settlement` fail-safe for unparseable profit on a confirmed-settled contract**
      In `deriv_to_client` (inside `deriv_websocket_proxy`), after the call to `strategy_execution.on_settlement(...)` at approximately line 980, add a guard: if `next_proposal is None` AND `strategy_execution.state is ExecutionState.WAITING_SETTLEMENT` (meaning `on_settlement` returned early because `outcome` was `None` and the state was not advanced), treat the outcome as a loss by calling `strategy_execution.on_settlement(str(contract_id), "lost", -1.0, next_request_id)` with a fresh `next_request_id` and a synthetic loss outcome. Log a warning via `logging.warning(...)` before doing so. This ensures the strategy never stalls indefinitely on a confirmed-settled contract with an unparseable profit field.
      No changes to `execution.py` or any public method signature. No callers of `on_settlement` are affected.
      Files: `app/deriv_gateway.py` (add `import logging` at top; add fail-safe in `deriv_to_client`)
      Verify: `python -m pytest -q` — all 187 tests pass; no new failures.

- [ ] 2. **Fix 2 — Capture `proposal_open_contract` subscription ID and send `forget` on settlement**
      In `deriv_websocket_proxy`, add a new closure variable `strategy_poc_sub_id: str | None = None` alongside the existing `pending_strategy_proposals` declaration (around line 717). In `deriv_to_client`, inside the `if strategy_execution is not None and provider_message.get("msg_type") == "proposal_open_contract":` block (around line 970): on the first message for a new contract (when `strategy_poc_sub_id is None`), capture `provider_message.get("subscription", {}).get("id")` and store it. When `settled == True`, before calling `strategy_execution.on_settlement(...)`, send `{"forget": strategy_poc_sub_id}` upstream (if `strategy_poc_sub_id` is not None), then clear `strategy_poc_sub_id = None`. The variable must be declared as `nonlocal` inside `deriv_to_client` because it is assigned (not just read) in a closure.
      Files: `app/deriv_gateway.py`
      Verify: `python -m pytest -q` — all 187 tests pass.

- [ ] 3. **Fix 7 — Error code granularity: retry retriable errors, include code in block reason**
      In `deriv_to_client`, replace the two `strategy_execution.block(...)` calls for proposal errors (around line 935) and buy errors (around line 958) with error-code-aware logic:
      - Extract `error_code = provider_message.get("error", {}).get("code", "")`.
      - For `"RateLimit"` or `"TemporaryError"`: apply exponential backoff (1s, 2s, 4s) and resend the original proposal or buy message upstream (up to 3 retries). Track retry count with a local counter scoped to each attempt. If retries are exhausted, call `strategy_execution.block(...)` with the error code in the reason string.
      - For all other codes: call `strategy_execution.block(f"Deriv rejected the strategy proposal: {error_code}.")` (or buy equivalent) — identical to today but with the code appended.
      The retry loop requires access to the `upstream` WebSocket and the original message. For proposals, the original message is reconstructable via `strategy_execution._proposal(request_id)` (already a method). For buys, the `on_proposal` return value (stored as it flows through the pipeline) must be accessible — since it is not currently stored, the simplest approach is: on a retriable buy error, call `strategy_execution._proposal` to get a fresh proposal (restarting from proposal phase) rather than retrying the buy directly, since the buy price quote may have changed during the backoff. Set state back to `WAITING_PROPOSAL` and resend a fresh proposal with a new request ID.
      Files: `app/deriv_gateway.py`
      Verify: `python -m pytest -q` — all 187 tests pass.

- [ ] 4. **Fix 5 — Add DEBUG log when `underlying_symbol` is absent in tick routing**
      Add `import logging` to `deriv_gateway.py` imports (if not already added in item 1). In `deriv_to_client` at the tick-routing block (around line 1015), after `symbol = tick.get("underlying_symbol") or tick.get("symbol")`, add:
      ```python
      if tick.get("underlying_symbol") is None:
          logging.debug("Tick missing 'underlying_symbol'; using 'symbol' field: %s", symbol)
      ```
      Apply the same pattern in `upstream_to_client` of `live_market_proxy` (around line 1234).
      Files: `app/deriv_gateway.py`
      Verify: `python -m pytest -q` — all 187 tests pass.

- [ ] 5. **Fix 3 — Credential expiry warning coroutine**
      Inside `deriv_websocket_proxy`, add a new inner async coroutine `credential_expiry_monitor()` alongside the existing `strategy_response_timeout_monitor`. The coroutine loops with `asyncio.sleep(60)`, checks `credential.expires_at - time.time()`, and if less than 300 seconds remain sends `{"type": "session_warning", "message": "Deriv session expires in 5 minutes. Reconnect to continue trading."}` to `websocket`. Add this coroutine's task to the `tasks` set passed to `asyncio.wait` at line 1079.
      Files: `app/deriv_gateway.py`
      Verify: `python -m pytest -q` — all 187 tests pass.

- [ ] 6. **Fix 1 — Reconnection loop with exponential backoff for both proxy endpoints**

      ### `deriv_websocket_proxy` (lines ~776–1092)
      Wrap the existing `async with websockets.connect(provider_url, ...) as upstream:` block and everything inside it in a `while True:` reconnect loop. Add a backoff counter (`reconnect_attempt = 0`) and a `max_reconnect_attempts = 5` constant before the loop. On each loop iteration:
      1. If `reconnect_attempt > 0`: compute delay `min(2 ** (reconnect_attempt - 1), 30) + random.uniform(0, 1)`, sleep, then re-fetch a fresh OTP using `credential` (call the same `client.post(otp endpoint)` pattern already present in the setup code — extract this into a helper or inline it). If the credential has expired or the OTP fetch fails, close the client WebSocket with a `4401` code and return. Validate the new URL the same way the original code does.
      2. Open `websockets.connect(provider_url_for_this_attempt, ...)`.
      3. On first connect (`reconnect_attempt == 0`): accept the client WebSocket (`await websocket.accept(...)`), initialize all local strategy variables, spawn tasks, and await `asyncio.wait`.
      4. On reconnect (`reconnect_attempt > 0`): do NOT call `websocket.accept()` again (already accepted). Reset `strategy_poc_sub_id = None`. Resubscribe to active tick subscriptions: if `strategy_subscribed_symbol` is not None, re-send `{"ticks": strategy_subscribed_symbol, "subscribe": 1}` and a new history request upstream. Do NOT re-subscribe to any open contracts. Spawn fresh tasks and await `asyncio.wait`.
      5. If `asyncio.wait` returns because the **client** WebSocket task (`client_to_deriv`) completed (i.e., the client disconnected), break out of the reconnect loop — no reconnect needed.
      6. If `asyncio.wait` returns because the **upstream** task (`deriv_to_client` or `strategy_response_timeout_monitor`) completed due to `ConnectionClosed`, increment `reconnect_attempt` and continue the loop.
      7. If `reconnect_attempt >= max_reconnect_attempts`, close client WebSocket with code 1011 and return.

      ### `live_market_proxy` (lines ~1117–1292)
      Apply the same reconnect loop pattern but simpler — no OTP re-fetch needed (uses the public WS endpoint `wss://api.derivws.com/trading/v1/options/ws/public`). On reconnect: resubscribe to all symbols currently in `subscribed_symbols` by resending `{"ticks": symbol, "subscribe": 1}` for each. Call `strategy_feed.disconnected()` on upstream drop before sleeping (as the existing code does). Cap at 5 reconnect attempts; on exhaustion close client WebSocket with code 1011.

      Files: `app/deriv_gateway.py` (add `import random` at top if not present)
      Verify: `python -m pytest -q` — all 187 tests pass.

---

## Edge cases and gotchas

**Fix 1 — task identity on reconnect:** `asyncio.wait` returns `done` and `pending` task objects. On reconnect, the new iteration creates new `asyncio.Task` objects. The old cancelled tasks from the previous iteration must be fully awaited/cancelled before creating new ones to avoid orphaned coroutines. Use `task.cancel(); await asyncio.gather(*pending, return_exceptions=True)` before the next loop iteration.

**Fix 1 — `websocket.accept()` idempotence:** FastAPI/Starlette will raise if `websocket.accept()` is called twice. The reconnect loop must track whether accept has already been called (via a boolean flag `ws_accepted = False`) and skip it on subsequent iterations.

**Fix 1 — `client_to_deriv` references `upstream` directly:** The inner coroutines `client_to_deriv` and `deriv_to_client` close over `upstream`. On reconnect, a new `upstream` object is produced by the new `websockets.connect(...)` context manager. The inner coroutines must be redefined on each reconnect iteration, or the `upstream` variable must be a mutable cell (e.g., a list `[upstream_ws]`) that the coroutines dereference. The cleanest approach is to redefine the inner coroutines inside the reconnect loop body — they already capture `upstream` by closure, so moving them inside the loop makes each iteration get fresh closures over the new connection.

**Fix 1 — strategy state on reconnect:** Variables like `strategy_execution`, `strategy_feed`, `strategy_frequency`, `pending_strategy_proposals`, `pending_strategy_buys`, `strategy_request_deadlines`, `valid_proposals`, `strategy_history_requests` must all survive reconnect (they describe the client's strategy session, not the upstream connection). Only connection-specific state (`strategy_poc_sub_id`, upstream subscription state) should be reset.

**Fix 2 — subscription ID field location:** The Deriv API returns the subscription ID in `provider_message.get("subscription", {}).get("id")` on the **first** `proposal_open_contract` message for a contract. Subsequent tick-update messages for the same contract also carry the same `subscription.id`, so capturing it on first arrival (when `strategy_poc_sub_id is None`) is sufficient. If `subscription` key is absent (API version difference), fall back to `provider_message.get("id")`.

**Fix 2 — forget ordering:** Send `{"forget": sub_id}` before calling `on_settlement`, to ensure the subscription is cleaned up even if `on_settlement` returns early.

**Fix 3 — coroutine cancellation:** When the client disconnects and tasks are cancelled, the `credential_expiry_monitor` coroutine must handle `asyncio.CancelledError` cleanly — it does so naturally since `asyncio.sleep` propagates cancellation. No special handling needed beyond the existing `task.cancel()` pattern.

**Fix 3 — `websocket.send_json` after client disconnect:** If the client has already disconnected when the warning fires, `websocket.send_json` will raise `WebSocketDisconnect`. Wrap the send in a `try/except WebSocketDisconnect` inside `credential_expiry_monitor` and return from the coroutine.

**Fix 4 — double call prevention:** The fail-safe in `deriv_to_client` only fires when `strategy_execution.state is ExecutionState.WAITING_SETTLEMENT` after `on_settlement` returned `None`. This state check ensures the fail-safe only fires once per contract, not on repeated `proposal_open_contract` messages.

**Fix 7 — retry buy vs fresh proposal:** Retrying a buy directly is unsafe because the `ask_price` quote from the proposal may have expired. On a retriable buy error, the safer path is to restart from proposal phase: reset state to `WAITING_PROPOSAL` (via `_block` then re-arm, or by directly manipulating state — prefer calling `strategy_execution.begin_signal` equivalent logic). Actually the cleanest approach: for retriable buy errors, call `strategy_execution.block(...)` first to reset state, then call `strategy_execution.begin_signal(new_request_id)` to start a fresh proposal cycle with backoff delay. This keeps `StrategyExecution`'s state machine valid.

**Fix 7 — nonlocal retry counter:** The retry logic inside `deriv_to_client` needs a mutable counter per in-flight request. Use a dict keyed by request_id: `retriable_attempts: dict[int, int] = {}` declared alongside `pending_strategy_proposals` at line ~717.

**No signature changes in `execution.py`:** `on_settlement`, `on_buy`, `on_proposal`, `begin_signal`, `block` — all unchanged. No callers outside `deriv_gateway.py` call these methods (confirmed by grep: only `tests/test_strategy_execution.py` and `deriv_gateway.py` reference `StrategyExecution`).

---

## Summary of files changed

| File | Fixes |
|---|---|
| `app/deriv_gateway.py` | 1, 2, 3, 4, 5, 7 |
| `app/strategy/execution.py` | none (fail-safe placed at call site in gateway) |

## Verification command

```
python -m pytest -q
```
Expected: all 187 existing tests pass; no regressions.
