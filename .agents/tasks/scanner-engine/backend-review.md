# Multi-market scanner engine with digit contract execution

This change adds an automated scanner that evaluates every viable digit contract across all subscribed symbols on each tick, scores them against the established 12%/14%/7% threshold rules, and streams the top-3 results to the client. Execution is extended so EVEN, ODD, MATCHES, and DIFFERS contracts can be traded from both the scanner output and direct user request. The implementation spans two commits: the initial feature build and a follow-up pass that addressed three prior review findings (EVEN/ODD qualifying-digit coherence, the direction-gate gap in `market_proxy.py`, and input validation strength).

The scoring logic, field contracts, and test results match the specification. 248 tests pass; two pre-existing failures are unrelated.

**Watch for:** `DIGITDIFF` vs Deriv's actual wire-format name (confirmed mismatch risk); EVEN/ODD barrier field accepted by `client_handler.py` without rejection when a user starts the strategy (possible silent misconfiguration); `StrategyExecution.status()` always reports the Group placeholder, not the override contract type (confirmed gap in observability for non-OVER/UNDER trades).

**Verdict**: NEEDS_CHANGES

---

## High-level view

The scanner is a pure function module with no I/O. It delegates all statistical logic to `evaluate_threshold_signal` and `evaluate_color_positions` from `digit_strategy`, keeping the scoring rules in one place. The fix commit eliminated the previous dual-path qualifying-digit computation for EVEN/ODD, which could have reported a different digit for `qualifying_digit` vs `confidence`.

`StrategyExecution` acquires two new surface areas: `contract_type_override` suppresses the `barrier_valid_for` guard for non-OVER/UNDER types, and `from_scan_result` maps a `ScanResult` to an execution object. For EVEN/ODD the barrier placeholder is 0 and passes validation only because the override path skips `barrier_valid_for`—that coupling is intentional and documented, but it means the barrier field in execution status messages will show 0 for EVEN/ODD trades, with no indication of the real contract type.

The `market_proxy.py` scanner feed runs on every tick, extracts the 1000-tick window from every tracked symbol, and fires `scanner_update` only when the top-ranked result changes. The deduplication key is `(symbol, contract_type, barrier)`, so a score change without a key change won't retrigger—acceptable.

`client_handler.py` gates direction validation to OVER/UNDER correctly, but the barrier validation runs unconditionally. A client requesting DIGITEVEN with `barrier: null` gets an unhelpful barrier error rather than a contract-type-appropriate one; a stray integer barrier value is silently discarded, inconsistent with how `_validate_proposal` handles the same case.

`DIGITDIFF` appears to match Deriv's documented contract type for "last digit differs", but no test sends a proposal to Deriv with that string and validates the response. A wrong name returns a Deriv API error with no local signal.

---

<details>
<summary>Issues (4)</summary>

1. **DIGITDIFF wire name** — Deriv's API contract type for "digit differs" is `DIGITDIFF` per their documentation, but this should be confirmed against a live proposal response before shipping. If the name is wrong, all DIFFERS trades will fail at the Deriv proposal step with no local error. Confirm with a test proposal or check Deriv's `contracts_for` endpoint output.

2. **`status()` reports placeholder, not real contract type** — `StrategyExecution.status()` always emits `direction: group.value` and `barrier: self.barrier`. For an EVEN trade this means the client sees `direction: "over"` and `barrier: 0` in every execution status message. Add `contract_type` to the `status()` payload so the client can render the actual trade type.

3. **EVEN/ODD strategy-start barrier field not rejected** — `client_handler._handle_strategy_start` validates that `barrier` is an integer in 0–9 unconditionally, but for DIGITEVEN/DIGITODD the barrier field is irrelevant and the user may send a misleading value that then appears in status messages. The validator should either reject the barrier field for EVEN/ODD (matching the `_validate_proposal` behaviour which rejects a barrier field on EVEN/ODD proposals) or silently zero it before construction, with a clear comment.

4. **No execution-path tests for new contract types** — `test_strategy_execution.py` has no cases for `contract_type_override`, `from_scan_result`, or the EVEN/ODD/MATCHES/DIFFERS proposal payload. The scanner unit tests cover scoring and ranking but not the round-trip from `ScanResult` through `from_scan_result` to a Deriv proposal JSON. A `from_scan_result` test for DIGITEVEN (barrier placeholder 0, no barrier in proposal) and DIGITDIFF (barrier in proposal) would catch regressions in the override path.

</details>

---

<details>
<summary>Details</summary>

### DIGITDIFF wire-format name

Every component uses `DIGITDIFF`, which matches Deriv's documented contract type for the "last digit differs" product. The risk is that no test actually sends a proposal to Deriv with that string and validates the response—a wrong name returns a Deriv API error with no local signal. Confirm via `contracts_for` or a dry-run proposal before shipping DIFFERS to production. Confidence: **likely** correct, not **confirmed**.

### Execution status observability gap

`StrategyExecution.status()` constructs its payload from `self.group.value` and `self.barrier`:

```python
return {
    "type": "strategy_execution_status",
    "direction": self.group.value,   # always "over" for EVEN/ODD/MATCHES/DIFFERS
    "barrier": self.barrier,         # 0 for EVEN/ODD; target digit for MATCHES/DIFFERS
    ...
}
```

For a running DIGITEVEN trade, the client receives `direction: "over", barrier: 0` in every status message. The `contract_type` property is available on the same object but never included in the status payload. This is a **confirmed** gap—the execution state machine is correct, but the status messages are misleading for every non-OVER/UNDER contract.

### EVEN/ODD barrier handling at strategy start

`_validate_proposal` in `client_handler.py` explicitly rejects a `barrier` field for EVEN/ODD proposals:

```python
if contract_type in {"DIGITEVEN", "DIGITODD"} and "barrier" in message:
    await websocket.send_json({"error": {"code": "INVALID_BARRIER", ...}})
    return False
```

The strategy-start path in `_handle_strategy_start` has no equivalent check. It reads `barrier = control.get("barrier", 5)` and validates it as an integer in 0–9 for all contract types, then passes it to `StrategyExecution` for EVEN/ODD as a placeholder `_barrier = 0` (overriding whatever the client sent). The result is that a client can send `barrier: 7` with `contract_type: DIGITEVEN`, receive no error, and have that value silently discarded. The inconsistency between proposal validation and strategy-start validation is a **confirmed** issue—not a security risk, but a confusing API surface that could mislead frontend developers.

### Test coverage gaps

The 6 new tests in `test_market_scanner.py` cover OVER threshold, EVEN threshold, DIFFERS depletion, sort order, `get_best_signal` returning None, and `get_best_signal` returning a HIGH-confidence result. That coverage is adequate for the scanner's public API.

What is not tested:

- `StrategyExecution.from_scan_result` for any contract type
- `StrategyExecution._proposal` for DIGITEVEN (should omit barrier) and DIGITDIFF (should include barrier)
- `client_handler._handle_strategy_start` with `contract_type` values other than DIGITOVER/DIGITUNDER
- `scan_all_contracts` with an invalid percentages map (the `_valid_pct_map` fast-reject path)
- The `scanner_update` message shape from `market_proxy.py`

The `from_scan_result` → `_proposal` path is load-bearing: a regression there would send malformed proposals to Deriv silently. **Confirmed** untested.

</details>

---

<details>
<summary>Files changed</summary>

| File | What changed |
|---|---|
| `app/strategy/market_scanner.py` | New module: `ContractSpec`, `ScanResult`, `score_contract`, `scan_all_contracts`, `get_best_signal`; second commit tightened `_valid_pct_map` and replaced dual-path EVEN/ODD logic with full delegation to `evaluate_threshold_signal` |
| `app/strategy/execution.py` | Added `contract_type_override` field, `from_scan_result` classmethod, `contract_type` property; `_proposal` now omits barrier for EVEN/ODD and sends it for MATCHES/DIFFERS |
| `app/deriv/client_handler.py` | Strategy start reads `contract_type`, validates against all 6 types, skips direction validation for non-OVER/UNDER, constructs execution with override |
| `app/deriv/market_proxy.py` | Imports scanner; after each tick extracts 1000-tick percentages per symbol (sample ≥ 50), calls `scan_all_contracts`, deduplicates by top key, sends `scanner_update`; direction gate updated to match `client_handler` |
| `tests/test_market_scanner.py` | 6 new tests covering OVER/EVEN/DIFFERS scoring, sort order, `get_best_signal` None and positive cases |

Full diff: `git -C d:\ai-tradingbridge diff HEAD~1...HEAD`

</details>
