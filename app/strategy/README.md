# TradeBridge Digit Strategy — Step 1

This package is an isolated, side-effect-free Python rules engine. It does not connect to Deriv, FastAPI routes, a database, or the UI, and it cannot place trades.

## Files
- `digit_strategy.py`: percentage thresholds, stability spread, group selection, lowest-digit trigger selection, and tick-sequence recognition.
- `../../tests/test_digit_strategy.py`: automated unit tests.

## Run tests from the repository root
```powershell
python -m unittest tests.test_digit_strategy -v
```

## Deliberately not implemented yet
- Deriv Digit Analytics feed integration (must be verified; a locally computed tick histogram is not automatically equivalent).
- Strategy session orchestration and post-initial-10-second window policy.
- Live safety-breach predicate.
- Trade submission/settlement integration.
- Tie-break for equal lowest percentages.

No trade execution is implemented in this step. Real-account execution must remain disabled until separately reviewed safety and reconciliation controls are ready.
