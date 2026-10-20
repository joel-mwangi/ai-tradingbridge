# TradeBridge Digit Strategy — Percentage Source Contract

The strategy accepts percentage snapshots from the backend's `digit_frequency`
payload on `/deriv/live/market`. This is the source of truth for TradeBridge's
displayed and strategy-consumed percentages.

**Important distinction:** these are TradeBridge-calculated observed-tick
frequencies over the most recent 1,000 valid tick points. They are not official
Deriv Digit Analytics percentages, are not predictions, and do not establish
that a digit is more likely on the next tick.

## Source contract

- Source identifier: `tradebridge_backend_tick_frequency`.
- Market: the payload's `symbol`, which must match the active strategy market.
- Window: `windows["1000"]`, with at least 2 and at most 1,000 valid observed ticks.
- Percentages: all ten digits, finite numeric values from 0 through 100.
- Observation timestamp: backend `calculated_at`, generated only when a history/tick response is processed.
- Event identity: market symbol + latest observed tick epoch + calculation timestamp + window.
- Receipt timestamp: assigned by the receiving process; it must be timezone-aware.
- Health validation: normalized snapshots must pass `PercentageFeedHealth` for source,
  market, freshness, timestamp ordering, duplicate IDs, and complete percentages.

The adapter rejects malformed data and does not synthesize percentage updates.
The feed consumer must mark disconnects, reset feed-health continuity after a
confirmed reconnect, and collect a fresh 10-second observation window. If the
source timestamp stalls, moves backwards, is stale, or the source/market changes,
the feed must fail closed.

## Tests

Run from the repository root:

```powershell
python -m pytest tests/test_market_frequency_adapter.py tests/test_feed_health.py tests/test_strategy_session.py -v
```

This source-contract step does not wire the strategy session to the live WebSocket
or place trades. Those are separate integration steps. Real-account execution
remains disabled.
