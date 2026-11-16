# Digit strategy engine hardened with threshold, reverse, and fast-pattern signals

The PR adds three new analytical constructs to `digit_strategy.py`: `evaluate_threshold_signal` for the community 12% entry rule, `evaluate_reverse_signal` for the DollarPrinter saturation flip, and `FastPatternTracker` for 2-tick LOW→HIGH confirmation. `GroupEvaluation` gains a `threshold_signal` field with a dataclass default, so all existing construction sites continue to work without modification. The frontend adds a full-width badge card to the qualification checklist that renders gracefully when the backend has not yet wired up the new fields. The backend commit passes its test suite (recorded in the commit message); the frontend commit verifies `tsc --noEmit` exits 0.

**Watch for:** `max(qualifying)` picks the *highest* digit index on a percentage tie, contradicting the comment "break ties by lowest digit index" — **confirmed** by running the tie-break with equal percentages. `FastPatternTracker` in stage 1 silently passes neutral digits (neither LOW nor HIGH) through without resetting the state machine; whether this matches the intended 2-tick contract is undocumented and unverified by any test — **confirmed** by reading the code path. The reverse signal test suite has no coverage for `Group.UNDER` — **confirmed** absence.

**Verdict**: NEEDS_CHANGES

---

## High-level view

`evaluate_threshold_signal` scans the *losing zone* of the contract — digits 0..barrier for OVER, digits barrier..9 for UNDER — for any entry at or above 12%, then ranks confidence by whether that digit is also the global Green or Blue. The scan zone semantics are correct for a mean-reversion entry: the zone that must become *less* frequent is the one that is currently oversaturated.

`evaluate_reverse_signal` checks whether the Green digit (global maximum) sits in the *winning zone* of the trade and is above the same 12% threshold. When the winning side is already saturated, the expectation is that mean reversion will suppress it, making the opposite contract more likely to pay. The confirming zone excludes the barrier digit itself for both directions; this is consistent with how the existing colour-position rules treat the barrier boundary.

`FastPatternTracker` is a two-state machine: state 0 waits for any LOW (Red or Yellow) digit, then state 1 waits for any HIGH (Green or Blue) digit. The colour membership sets are captured from a percentages snapshot at `arm()` time, so the classifier does not drift between ticks. The neutral-digit handling in state 1 is a gap: a digit that is neither LOW nor HIGH leaves the tracker in state 1 indefinitely, allowing the HIGH trigger to fire many ticks later after multiple neutral digits have passed. The docstring calls it "2-tick confirmation" but the implementation does not enforce a 2-tick window.

`GroupEvaluation`'s new field uses a dataclass default correctly positioned after all required fields. The four early-exit construction paths in `choose_group` and `evaluate_group` all omit the field and receive the default `ThresholdSignal(False, None, None, "NONE", "Not evaluated.")`, which is the correct fallback.

The frontend `StrategyStatus.qualification` type is extended with seven optional fields, all typed `| null`, so existing consumers that never set them see no change. The card itself gates all digit/percentage/confidence display on `threshold_triggered === true` and renders a safe "threshold not met" message when the backend emits nothing, confirming the graceful-degradation claim in the commit message.

---

<details>
<summary>Issues (4)</summary>

1. **Tie-break picks highest digit, not lowest** — `max(qualifying)` on `(pct, digit)` tuples breaks ties by *maximum* digit index, but the comment says "break ties by lowest digit index." Fix with `max_pct = max(p for p,_ in qualifying); best_digit = min(d for p,d in qualifying if p == max_pct)`.

2. **FastPatternTracker neutral-digit pass-through in stage 1** — A digit that is neither LOW nor HIGH while in stage 1 leaves stage at 1 and arm active, so the HIGH trigger can fire arbitrarily many ticks after the LOW digit. The class docstring calls this "2-tick confirmation" but enforces no tick-distance limit. Document the intended behaviour or add a neutral-digit reset to enforce the 2-tick contract.

3. **No test for `evaluate_reverse_signal` with `Group.UNDER`** — The three `ReverseSignalTests` cases all use `Group.OVER`. The UNDER confirming zone (`range(0, barrier)`) follows different code path and is not exercised; a Green digit at index 0 with barrier=5 on a UNDER group is unverified.

4. **No test for FastPatternTracker neutral-digit pass-through** — There is no test verifying what happens when a neutral digit arrives in stage 1. Because the behaviour is undocumented (see issue 2), there is no way to know whether the current pass-through is intentional until a test pins it.

</details>

---

<details>
<summary>Details</summary>

## Tie-break direction in `evaluate_threshold_signal`

The qualifying list is built as `(float, int)` tuples — `(pct, digit)` — and selected with `max(qualifying)`. Python's `max` on tuples uses lexicographic comparison, so ties on the first element are broken by the second: the *largest* digit index wins. The comment directly above says "break ties by lowest digit index." For the common case where one digit is clearly the highest percentage, this makes no difference. For equal-percentage ties across multiple digits in the scan zone, the function returns a different digit than the comment promises. The fix is two lines:

```python
max_pct = max(pct for pct, _ in qualifying)
best_digit = min(d for pct, d in qualifying if pct == max_pct)
```

No existing test exercises a tie because every test fixture gives one digit a distinct maximum. This means the bug passes all current tests.

## `FastPatternTracker` neutral-digit state in stage 1

In `on_tick` when `_stage == 1`:

```python
if digit in self._high_digits:
    ...  # complete
if digit in self._low_digits:
    self._stage = 0  # reset to stage 0
return False
```

A digit that falls in neither set hits only `return False`, leaving `_stage = 1` and `_armed = True`. The tracker will stay in state 1 until a HIGH digit eventually arrives, regardless of how many neutral digits intervene. For genuinely fast markets this may be acceptable, but the class contract ("2-tick fast confirmation") implies the HIGH should follow immediately after the LOW. The five `FastPatternTrackerTests` cases use back-to-back ticks and never test a neutral digit in between, so this behaviour has no specification and no test.

## `evaluate_reverse_signal` UNDER confirming zone gap

All three `ReverseSignalTests` pass `Group.OVER`. The UNDER branch uses `confirming_zone = frozenset(range(0, barrier))` and `reverse_label = "OVER"` — a path that is completely untested. The most useful missing case: Green digit at index 0 with barrier 5 on a UNDER group, which should trigger `suggested = True`.

</details>

---

<details>
<summary>File map</summary>

| File | Change |
|---|---|
| `app/strategy/digit_strategy.py` | New `ContractType` enum, `ThresholdSignal` and `ReverseSignal` dataclasses, `evaluate_threshold_signal`, `evaluate_reverse_signal`, `FastPatternTracker`; new `threshold_signal` field on `GroupEvaluation`; `evaluate_group` wires threshold signal |
| `tests/test_digit_strategy.py` | New `ThresholdSignalTests`, `ReverseSignalTests`, `FastPatternTrackerTests`, `GroupEvaluationThresholdSignalTests` |
| `src/components/trading/DigitStrategyPanel.tsx` | `StrategyStatus.qualification` extended with 7 optional fields; full-width 12% Threshold Signal badge card inserted in qualification checklist |

Full backend diff: `git -C d:\ai-tradingbridge\.worktrees\harden-digit-threshold diff main...HEAD`  
Full frontend diff: `git -C D:\tradebridge diff HEAD~1 HEAD -- src/components/trading/DigitStrategyPanel.tsx`

</details>
