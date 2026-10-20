# TradeBridge Digit Strategy — Step 1

This package is an isolated, side-effect-free Python rules engine. It does not connect to Deriv, FastAPI routes, a database, or the UI, and it cannot place trades.

## Files
- `digit_strategy.py`: percentage thresholds, stability spread, group selection, lowest-digit trigger selection, tick-sequence recognition, and validation for timestamped initial-window snapshots.
- `../../tests/test_digit_strategy.py`: automated unit tests.

## Run tests from the repository root
```powershell
python -m unittest tests.test_digit_strategy -v
```

## Timestamped initial-window validation

`validate_initial_observation_window` checks that:
- the session start and evaluation timestamps are timezone-aware;
- the full configured initial window (10 seconds by default) has elapsed;
- at least two actual source snapshots exist (one snapshot alone cannot prove stability);
- snapshots have strictly increasing, timezone-aware source timestamps within the initial window;
- every snapshot contains all ten finite digit percentages in the 0–100 range.

This is a validation helper only. It does **not** verify that the upstream source is Deriv Digit Analytics, prove that no updates were missed, or define a post-window rolling policy. The caller must not synthesize updates or use an unverified source.

## Deliberately not implemented yet
- Deriv Digit Analytics feed integration (must be verified; a locally computed tick histogram is not automatically equivalent).
- Strategy session orchestration and post-initial-10-second window policy.
- Live safety-breach predicate.
- Trade submission/settlement integration.
- Tie-break for equal lowest percentages.

No trade execution is implemented in this step. Real-account execution must remain disabled until separately reviewed safety and reconciliation controls are ready.
