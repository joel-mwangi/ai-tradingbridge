# Deriv API Research Report
## Ensuring the TradeBridge Strategy Bot Works Reliably

*Investigation date: 2025. Sources: developers.deriv.com (official), legacy-docs.deriv.com, codebase review.*

---

## Executive Summary

The TradeBridge codebase already implements a WebSocket proxy architecture with solid credential hygiene, demo-only gating, and strategy execution logic. However, several meaningful reliability gaps exist that will cause the bot to malfunction or silently fail in production. The most critical are: **no reconnection logic** on either the upstream Deriv WebSocket or the OTP-gated trading session, **no handling of the 120-second OTP expiry window**, **no explicit `forget` of stale subscriptions**, and **a `proposal_open_contract` field-name dependency (`is_sold`) that does not match the new Deriv API's confirmed field set**. Each finding is detailed below with actionable fixes.

---

## 1. Deriv WebSocket API Best Practices (Connection Management, Keepalive, Auth Flow)

### Summary

Deriv's official best-practices guide ([developers.deriv.com/docs/best-practices](https://developers.deriv.com/docs/best-practices/)) mandates:

1. **One shared connection, reused for all requests.** Open a single WebSocket and multiplex requests over it using `req_id` to correlate responses. Opening one connection per call is explicitly discouraged.
2. **Exponential backoff on reconnect.** When the connection drops, retry with delays of 1s, 2s, 4s, 8s (capped) plus random jitter. Do not retry in a tight loop.
3. **Periodic ping for keepalive.** Send `{"ping": 1}` periodically to detect silent disconnects early. The `ping` call does not draw on any rate-limit budget (it is exempt from all counters).
4. **Single-use OTP for authenticated sessions.** The `/trading/v1/options/accounts/{accountId}/otp` REST endpoint returns a ready-to-use `wss://` URL with an OTP query parameter. The OTP is **valid for 120 seconds and is single-use**. The WebSocket connection must be opened promptly after fetching it.
5. **Authorize once, at connection start.** For the legacy public WS endpoint (`wss://ws.binaryws.com/websockets/v3`), send `{"authorize": "<token>"}` before making account-scoped requests. For the new Options API (`wss://api.derivws.com/trading/v1/options/ws/demo`), authentication is baked into the OTP URL — no separate `authorize` message is needed.
6. **Re-authenticate and re-subscribe after reconnect.** After a reconnect, all prior subscriptions are gone from the server's perspective. The client must resend every active subscription, including tick streams and open contract monitors.

### Evidence in codebase

`deriv_gateway.py` (`deriv_websocket_proxy`):
- The upstream connect call uses `ping_interval=20` — correct for keepalive.
- No reconnection logic exists anywhere. If the upstream `websockets.connect()` drops mid-session, the `asyncio.wait` exits and the entire client WebSocket session is closed without an attempt to reconnect and restore subscriptions.
- The `live_market_proxy` endpoint has the same gap; it also calls `strategy_feed.disconnected()` on `ConnectionClosed`, but does not reconnect.

### Recommendations

- Wrap the `websockets.connect()` blocks in both proxy endpoints in a reconnection loop with exponential backoff (1s → 2s → 4s → 8s, capped at 30s, plus random jitter up to 1s).
- On reconnect for `deriv_websocket_proxy`: re-fetch a fresh OTP (the prior one was consumed), open a new connection, then resend all active tick subscriptions and re-subscribe to any open contracts being monitored.
- On reconnect for `live_market_proxy`: resubscribe to all symbols that were in `subscribed_symbols` at the time of the drop.
- Keep `ping_interval=20` in `websockets.connect()` — this is already correct and covers the keepalive requirement.

---

## 2. Rate Limits, Subscription Limits, and Trade Constraints

### Summary

Deriv imposes **shared per-minute and per-hour budgets** on the WebSocket API ([developers.deriv.com/docs/limits](https://developers.deriv.com/docs/limits/)):

| Calls grouped together | Per minute | Per hour |
|---|---|---|
| `proposal`, `proposal_open_contract`, `buy`, `sell` | 360 | 14,400 |
| `balance`, `statement` | 100 | 2,000 |
| `portfolio`, `profit_table` | 30 | 1,500 |
| All other calls | 220 | 14,400 |
| `ping` | 10/sec (not budgeted) | — |

Key points:
- **`proposal` and `buy` share the same 360/min budget.** A bot that requests a fresh proposal before each buy uses 2 credits per trade cycle.
- The bot's sequential 3-buy strategy (proposal → buy → settle → proposal → …) at moderate signal frequency is well within these limits.
- **Rate limits are dynamic.** Calling `{"website_status": 1}` returns the current `api_call_limits` field, which is the authoritative source. The figures above may change without notice.
- **Subscription limit:** The `live_market_proxy` in the codebase hard-limits `subscribed_symbols` to 256, which is a reasonable self-imposed guard. Deriv does not publish a hard subscription limit, but subscribing to hundreds of symbols on one connection is discouraged.
- **Digit contract constraints:** Duration must be 1–10 ticks (`duration_unit: "t"`). DIGITOVER barrier valid range: 0–8. DIGITUNDER barrier valid range: 1–9. Barriers 0 and 9 for the opposite direction are rejected by Deriv (no winning outcome possible). The gateway already enforces these correctly.

### Evidence in codebase

`deriv_gateway.py` (`client_to_deriv`):
- Validates barrier ranges for DIGITOVER (≤8) and DIGITUNDER (≥1) — correct.
- Validates `duration_unit == "t"` and `1 <= duration <= 10` — correct.
- No rate-limit backoff logic is implemented; if Deriv returns a rate-limit error, the strategy just blocks.

### Recommendations

- Add handling for Deriv error responses with rate-limit codes in `deriv_to_client`. When a `proposal` or `buy` response carries an error, inspect the `error.code` field. If it indicates throttling, apply an exponential backoff before retrying rather than immediately blocking the strategy.
- Optionally, on startup, call `{"website_status": 1}` and log the returned `api_call_limits` so the deployed rate is always known.

---

## 3. Digit Contracts (Over/Under) — Request/Response Fields and Contract Lifecycle

### Summary

**Proposal request fields (DIGITOVER / DIGITUNDER):**
```json
{
  "proposal": 1,
  "amount": 1.0,
  "basis": "stake",
  "contract_type": "DIGITOVER",
  "currency": "USD",
  "duration": 1,
  "duration_unit": "t",
  "underlying_symbol": "1HZ100V",
  "barrier": 5,
  "req_id": 12345
}
```
- `barrier` must be an integer 0–9 (send as integer, not string — both are accepted but integer is safer).
- `underlying_symbol` is the correct field name for the new Options API. The legacy API used `symbol`; the gateway validation code checks both (`message.get("underlying_symbol", message.get("symbol"))`).
- `basis: "stake"` means `amount` is your stake, not payout.

**Buy request:**
```json
{
  "buy": "<proposal_id>",
  "price": <ask_price_from_proposal>,
  "req_id": 99999
}
```
- `price` is the **maximum** you will pay. If the ask moves above this between quote and execution, the buy is rejected. Passing `ask_price` from the proposal is the correct behavior — the gateway already does this.

**Contract lifecycle events:**
1. `{"msg_type": "proposal"}` — contains `proposal.id` and `proposal.ask_price`. The proposal is short-lived; buy against it promptly.
2. `{"msg_type": "buy"}` — contains `buy.contract_id`. Subscribe to updates: `{"proposal_open_contract": 1, "contract_id": <id>, "subscribe": 1}`.
3. `{"msg_type": "proposal_open_contract"}` — pushed on every tick and at settlement. Key settlement fields:
   - `is_sold`: `0` or `1` (integer or boolean depending on API version)
   - `is_expired`: `0` or `1`
   - `status`: `"open"`, `"won"`, `"lost"`, `"sold"`
   - `profit`: the P&L (now returns `string | number` in the new API — see breaking change below)
   - `contract_id`, `contract_type`, `currency` are now **required** fields in the new API response.
4. When `is_sold == 1` (or status is `"won"` / `"lost"`), the contract is settled. Call `{"forget": "<subscription_id>"}` to release the stream.

**New API breaking changes (from developers.deriv.com/comparison/proposal-open-contract/):**
- `profit`, `bid_price`, `buy_price`, `current_spot` now return `string | number` (not just `number`).
- `payout` is now always a `string`.
- `sell_spot` and `sell_spot_time` are deprecated; use `exit_spot` and `exit_spot_time`.
- Display fields `display_value`, `exit_tick_display_value`, `sell_spot_display_value` have been removed.

**Tick feed for digit strategies:**
- Subscribe: `{"ticks": "1HZ100V", "subscribe": 1}`
- Each tick message: `{"msg_type": "tick", "tick": {"quote": 1234.56, "epoch": 1700000000, "pip_size": 2, ...}}`
- Last digit is computed from `quote` using `pip_size`: `round(quote * 10^pip_size) % 10`.
- The gateway computes this via `extract_last_digit` in `market_analysis.py` — correct approach.

### Evidence in codebase

`execution.py` (`_proposal`):
- Uses `"underlying_symbol"` — correct for the new API.
- Sends `barrier` as integer — correct.

`execution.py` (`on_settlement`):
- Checks `status` in `{"lost", "loss", "won", "win", "sold"}` — this covers the new API status strings.
- Reads `profit` via `float(profit)` — this will **fail** if Deriv returns `profit` as a string (breaking change). The `float()` call on a numeric string like `"1.23"` actually works in Python, so this is not a hard failure, but it is fragile against non-numeric strings.
- Settlement detection in `deriv_to_client` checks `is_sold in (1, True, "1")` — reasonable, but `"1"` as a string is an unusual guard; the new API returns integer `0`/`1`.

### Recommendations

- In `on_settlement`, wrap `float(profit)` in a try/except (already done) and also handle the case where `profit` is a string like `"1.23"` or `"-0.50"` — this already works with Python's `float()` but add a comment explaining the string/number duality.
- In `deriv_to_client`, after buy, subscribe to `proposal_open_contract` with the `subscribe: 1` flag — the gateway does this already via `on_buy` return value. Confirm `forget` is called when `is_sold` is detected (currently it is not — see Gap 3 below).
- Be aware that the legacy `sell_spot`/`sell_spot_time` fields are deprecated; if any downstream code reads them, migrate to `exit_spot`/`exit_spot_time`.

---

## 4. Common Failure Modes for Bots on Deriv

### 4.1 Dropped Connections

**Cause:** Deriv connections can drop due to network events, server-side maintenance, or idle timeout. The `websockets` library raises `ConnectionClosed` on drop.

**Current state:** Both `deriv_websocket_proxy` and `live_market_proxy` catch `ConnectionClosed` and exit cleanly but do **not** reconnect. The client WebSocket is also closed, ending the entire session.

**Recommendation:** Implement reconnect-with-backoff inside the proxy loop. For the trading proxy, reconnecting requires a new OTP fetch before reconnecting (the original OTP was consumed at the initial connection).

### 4.2 Stale Subscriptions / Subscription Leaks

**Cause:** If the upstream connection drops and reconnects, the server has no memory of prior subscriptions. The client side thinks it is still receiving ticks and contract updates, but the upstream is silent. The strategy session will starve of tick data and eventually block via feed-staleness checks, but the user gets no clear error message about why.

Additionally, when a `proposal_open_contract` subscription is established for a settled contract, `forget` is never explicitly called. Over many trade cycles, this leaks subscription slots and adds unnecessary upstream traffic.

**Evidence:** `execution.py` (`on_buy`) returns `{"proposal_open_contract": 1, "contract_id": ..., "subscribe": 1}` which is sent upstream. But after settlement is detected in `deriv_to_client`, there is no corresponding `{"forget": <sub_id>}` sent upstream. The subscription ID (`id` field in the `proposal_open_contract` response) is never captured.

**Recommendation:**
- Capture the `id` (subscription ID) from the first `proposal_open_contract` response for each contract.
- When settlement is detected (`is_sold == 1`), send `{"forget": "<subscription_id>"}` upstream before proceeding to the next proposal.
- On reconnect, replay all active subscriptions.

### 4.3 OTP / Authorization Token Expiry

**Cause:** OAuth access tokens are **short-lived (typically 3600 seconds / 1 hour)**. The OTP used to open the trading WebSocket is **single-use and expires in 120 seconds** if not consumed. If a user's session runs near the OAuth token's expiry boundary, a reconnect attempt will fail because `_owned_credential()` will raise a 401 (the credential is expired).

**Evidence:** `_owned_credential()` in `deriv_gateway.py` correctly checks `credential.expires_at <= time.time()` and deletes/raises 401. But the WebSocket proxy endpoints do not monitor the token expiry during an active session. A session that is 58 minutes old can continue trading, but any reconnect after the 60-minute mark will fail.

**Recommendation:**
- In the proxy loop, track when the current OAuth credential expires. Add a background task or pre-reconnect check that fetches a fresh OTP using the current credential before it expires. If the credential itself is near expiry, close the session gracefully and send the client an `{"error": {"code": "SESSION_EXPIRING", "message": "Reconnect Deriv before the session expires."}}` message.
- Consider adding a credential-refresh endpoint that the frontend can call to exchange a refresh token for a new OAuth access token, store it, and keep the session alive.

### 4.4 Error Codes to Handle

Based on Deriv documentation and codebase patterns:

| Scenario | What to expect | Correct handling |
|---|---|---|
| Proposal rejected | `{"error": {"code": "ContractBuyValidationError", ...}}` | Block strategy, send user status, call `unlock_execution()` |
| Buy rejected | `{"error": {"code": "PurchaseError", ...}}` or `"InvalidBuyPrice"` | Same as above |
| Rate limit exceeded | `{"error": {"code": "RateLimit", ...}}` | Exponential backoff, retry |
| Token scope error | `{"error": {"code": "PermissionDenied", ...}}` / HTTP 403 | Surface to user, require re-auth |
| Expired OTP / unauthenticated WS | HTTP 401 at handshake (before WS upgrade) | Re-fetch OTP, reconnect |
| Symbol unavailable | `{"error": {"code": "InvalidSymbol"}}` | Surface to user |
| Contract already sold | `{"error": {"code": "ContractIsSold"}}` | Update internal state, clean up subscription |

The current `deriv_to_client` handler checks `provider_message.get("error")` for proposals and buys but only calls `strategy_execution.block(...)` without inspecting the specific error code. This means rate-limit errors and symbol errors are treated identically to validation errors — both permanently block the strategy instead of allowing a retry where appropriate.

### 4.5 Silent Data Gap / Feed Stale

**Cause:** The tick feed from Deriv may pause (symbol halted, market closed, connection issue) while the WebSocket technically remains open. The `PercentageFeedHealth` class uses `TRADEBRIDGE_STRATEGY_FEED_MAX_AGE_SECONDS` (default 5s) to detect this, which is appropriate for 1s synthetic indices. For slower symbols this could produce false blocks.

**Recommendation:** The 5-second default is good for 1HZ100V and similar 1-tick-per-second synthetics. If the strategy is ever extended to support slower assets, make this configurable per-symbol rather than globally.

---

## 5. Testing and Sandbox Options

### Demo Accounts (Recommended approach)

Deriv provides full demo accounts with virtual money that use **identical infrastructure** to real accounts — the same WebSocket API, same contract types, same pricing engine, same error codes. This is the correct sandbox for bot testing.

- Demo account WebSocket: `wss://api.derivws.com/trading/v1/options/ws/demo` (requires OTP from the `/otp` endpoint).
- The TradeBridge backend already **enforces demo-only execution** in `activate_account_session()` and `deriv_websocket_proxy()` by checking `account_type == "demo"` and returning a 403 for real accounts.
- Real-account execution is explicitly disabled at the code level with the comment: *"Real-account execution remains disabled until server-side risk limits and order reconciliation are enforced."*

### Public Market Data (No Auth)

For testing tick subscriptions and digit frequency logic without credentials:
- `wss://api.derivws.com/trading/v1/options/ws/public` — supports `ticks`, `ticks_history`, `active_symbols`, `contracts_for`, and `forget`.
- Used by `live_market_proxy` in the codebase — correct.

### No Separate Testnet

Deriv does **not** provide a separate testnet environment (no `testnet.deriv.com` or staging WebSocket endpoint). The demo account is the only sandbox option. All demo trades settle against a virtual balance but go through the real Deriv trading infrastructure, so the bot gets realistic latency, error behavior, and contract lifecycle events.

### Integration Testing Approach

Given the lack of a separate testnet:
1. Run the strategy against a demo account with a small stake (`TRADEBRIDGE_DEMO_MAX_STAKE` default is 1000 demo units).
2. Test reconnect behavior by intentionally dropping network connectivity.
3. Test token expiry by setting a short `expires_in` in the credential store and verifying the 401 path works.
4. Test rate limits by sending rapid-fire proposals and verifying the backoff handler triggers correctly.

---

## 6. Gaps and Risks in the Current Codebase

This section cross-references the code findings with Deriv API behavior.

### Gap 1 — No Reconnection Logic (Critical)

**File:** `deriv_gateway.py`, both `deriv_websocket_proxy` and `live_market_proxy`.

**Risk:** Any dropped upstream WebSocket (network blip, Deriv server maintenance, idle timeout) terminates the entire user session. The strategy is stuck until the user manually reconnects from the frontend.

**Specific code location:** The `asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)` block that handles task completion does not attempt to reconnect `upstream` before closing `websocket`.

**Fix:** Wrap the `websockets.connect(...)` and the task loop in a retry loop. For `live_market_proxy`, simply reconnect and resubscribe to `subscribed_symbols`. For `deriv_websocket_proxy`, also re-fetch the OTP (the previous one is consumed), then restore tick subscriptions but **not** any open contract subscriptions (those would need portfolio reconciliation to restore correctly).

---

### Gap 2 — No Subscription Cleanup / `forget` After Settlement (High)

**File:** `deriv_gateway.py`, `deriv_to_client` handler; `execution.py`, `on_buy`.

**Risk:** Every contract cycle subscribes to `proposal_open_contract` but never sends `{"forget": "<sub_id>"}`. Over many strategy runs, the upstream connection accumulates leaked subscriptions, increasing message volume and approaching Deriv's undocumented subscription limits.

**Specific code:** `on_buy()` returns `{"proposal_open_contract": 1, "contract_id": ..., "subscribe": 1}`, which the gateway sends upstream. The `id` field in the first `proposal_open_contract` response (the subscription ID) is never captured, so `forget` cannot be called.

**Fix:**
1. When `deriv_to_client` receives the first `proposal_open_contract` message for a new subscription, capture `provider_message.get("subscription", {}).get("id")` or `provider_message.get("id")`.
2. Store this `sub_id` in `strategy_execution`.
3. When settlement is detected (`settled == True`), send `{"forget": sub_id}` upstream before calling `strategy_execution.on_settlement(...)`.

---

### Gap 3 — `underlying_symbol` Field on Tick (Medium)

**File:** `deriv_gateway.py`, `deriv_to_client` tick handler.

**Risk:** The code reads `tick.get("underlying_symbol") or tick.get("symbol")` to identify the symbol from tick messages. In the new Deriv Options API, the field name in the tick object is `symbol` (not `underlying_symbol`). If Deriv stops returning `underlying_symbol`, the symbol match against `strategy_subscribed_symbol` will fail and ticks will not be routed to the strategy.

**Evidence:** `deriv_to_client` in `deriv_websocket_proxy`:
```python
symbol = tick.get("underlying_symbol") or tick.get("symbol")
if symbol == strategy_subscribed_symbol:
```

**Fix:** Test which field the new API actually returns by logging a few tick messages in staging. If the new API consistently uses `symbol`, update the primary key. Keep `underlying_symbol` as a fallback for backwards compatibility.

---

### Gap 4 — `profit` Field Type Assumption in Settlement (Medium)

**File:** `execution.py`, `_outcome` and `on_settlement`.

**Risk:** The new Deriv API returns `profit` as `string | number`. `float("1.23")` works in Python, but `float(None)` returns `TypeError` (already handled). If Deriv sends an unexpected non-numeric string in the future, `float()` will raise `ValueError`, which is caught and returns `None` — causing `on_settlement` to treat the contract as still open and blocking the strategy indefinitely.

**Evidence:** `execution.py` `_outcome`:
```python
try:
    numeric_profit = float(profit)
except (TypeError, ValueError):
    return None
```
Returning `None` when profit is unparseable means the `if outcome is None: return None` path in `on_settlement` is taken, and the strategy neither wins/loses nor proceeds — it waits forever for another settlement event that will never come.

**Fix:** When `outcome is None` after settlement fields (`is_sold`, `status`) indicate the contract is actually settled, log a warning and treat it as a loss (fail safe). Do not indefinitely stall waiting for a cleaner settlement event.

---

### Gap 5 — No Credential Expiry Monitoring During Active Sessions (Medium)

**File:** `deriv_gateway.py`, `deriv_websocket_proxy`.

**Risk:** OAuth access tokens expire after ~3600 seconds. An active WebSocket session started 59 minutes ago will keep working, but any reconnect attempt will fail immediately with a 401 because `_owned_credential()` will detect the expired token. The user gets a sudden disconnect without explanation.

**Fix:** Add a background coroutine that runs inside the proxy session and checks `credential.expires_at` every 60 seconds. When the token is within, say, 300 seconds of expiry, send a warning message to the client:
```json
{"type": "session_warning", "message": "Deriv session expires in 5 minutes. Reconnect to continue trading."}
```
This gives the frontend time to show a reconnect prompt before the bot is silently cut off.

---

### Gap 6 — _ws_tickets Dictionary is Process-Local (Low/Deployment Risk)

**File:** `deriv_gateway.py`, `_ws_tickets`.

**Risk:** `_ws_tickets: dict[str, tuple[str, float]] = {}` is a module-level in-process dictionary. In a multi-worker deployment (e.g., `gunicorn -w 4` or multiple Docker replicas), a ticket issued by worker A cannot be consumed by worker B. This silently breaks WebSocket connections with a 4401 close code.

**Fix:** Use a shared store (Redis, Supabase, or any external KV) for the ticket map, or ensure the deployment is configured with exactly one worker per instance and sticky routing for the WebSocket upgrade request. Document this constraint clearly.

---

### Gap 7 — Error Code Granularity in Strategy Blocking (Low)

**File:** `deriv_gateway.py`, `deriv_to_client`.

**Risk:** When a proposal or buy is rejected by Deriv, the strategy calls `strategy_execution.block(...)` regardless of the error type. Rate-limit errors, symbol errors, and validation errors all result in permanent strategy blockage. Rate-limit errors should trigger a retry with backoff, not a permanent block.

**Fix:** Inspect `provider_message.get("error", {}).get("code")` in the proposal/buy error handlers. For retriable errors (rate limit, temporary system error), implement backoff-and-retry. For non-retriable errors (invalid symbol, insufficient funds, out-of-hours trading), block the strategy and surface the specific error code to the user.

---

## Conclusions and Priority Order

| Priority | Gap | Fix complexity |
|---|---|---|
| 1 (Critical) | No reconnection on upstream WS drop | Medium — reconnect loop + OTP re-fetch |
| 2 (High) | `proposal_open_contract` subscription never `forget`-ed | Low — capture sub_id, send forget on settlement |
| 3 (Medium) | Credential expiry not monitored during active sessions | Low — background expiry-check coroutine |
| 4 (Medium) | `profit` field stall when `outcome is None` on settled contract | Low — fail-safe to loss on confirmed settlement |
| 5 (Medium) | `underlying_symbol` vs `symbol` in tick field | Low — test and confirm correct field name |
| 6 (Low) | `_ws_tickets` is process-local, breaks multi-worker | Medium — shared KV store or sticky routing |
| 7 (Low) | Error code granularity in strategy blocking | Low — switch on error code, retry rate-limit errors |

The core trading logic (barrier validation, contract type mapping, sequential buy discipline, settlement detection) is well-implemented. The reliability risks are in connection lifecycle management and subscription hygiene — areas that only manifest under production conditions (long sessions, network instability, multi-worker deployments).

---

*Content was rephrased for compliance with licensing restrictions. Sources: [developers.deriv.com/docs/best-practices](https://developers.deriv.com/docs/best-practices/), [developers.deriv.com/docs/limits](https://developers.deriv.com/docs/limits/), [developers.deriv.com/docs/workflows](https://developers.deriv.com/docs/workflows/), [developers.deriv.com/docs/options/websocket](https://developers.deriv.com/docs/options/websocket/), [developers.deriv.com/comparison/proposal-open-contract](https://developers.deriv.com/comparison/proposal-open-contract/), [legacy-docs.deriv.com/docs/digit-overunder](https://legacy-docs.deriv.com/docs/digit-overunder).*
