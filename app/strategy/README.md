# TradeBridge Digit Strategy

This package contains a side-effect-free rules engine and normalized feed contract.
It does not connect directly to Deriv or submit trades.

## Files
- `digit_strategy.py`: thresholds, stability spread, group selection, trigger digit, and pattern recognition.
- `feed_health.py`: fail-closed source/market/freshness/order/duplicate validation and disconnect recovery state.
- `market_frequency_adapter.py`: normalizes the backend's `digit_frequency` payload from `/deriv/live/market`.
- `session.py`: fresh-session lifecycle, initial 10-second observation window, rolling 10-second reassessment, group selection, and pattern tracking.

## Percentage source

The configured source is `tradebridge_backend_tick_frequency`, using
`windows["1000"]` from the backend's observed tick histogram. These are
TradeBridge-calculated frequencies from the last 1,000 valid observed tick points,
not official Deriv Digit Analytics percentages and not predictions.

The adapter rejects unknown sources, missing symbols/windows, insufficient or
impossible sample sizes, incomplete/non-finite/out-of-range percentages, invalid
timestamps, and naive receipt timestamps. Each normalized snapshot carries a
market-scoped event ID derived from the latest observed tick and calculation time.
Every snapshot must still pass `PercentageFeedHealth`; matching a source ID does
not independently authenticate the transport.

The consumer must mark disconnects and reset feed-health continuity after a
confirmed reconnect. It must then rebuild the full 10-second observation window.
Never synthesize percentage updates or silently switch source types.

## Tests

```powershell
python -m pytest tests/test_market_frequency_adapter.py tests/test_feed_health.py tests/test_strategy_session.py -v
```

## Not implemented in this step

- Live WebSocket-to-session orchestration and UI state updates.
- Live safety-breach predicate.
- Trade submission/settlement and purchase-acknowledgement reconciliation.
- Tie-break for equal lowest percentages.

No trade execution is implemented here. Real-account execution remains disabled
until separately reviewed safety and reconciliation controls are complete.
