# TradeBridge Digit Strategy — Percentage Source and Live Feed

The strategy consumes backend-calculated `digit_frequency` snapshots emitted by
`/deriv/live/market`. This is the source of truth for TradeBridge's displayed
and strategy-consumed percentages.

**Important distinction:** these are TradeBridge-calculated observed-tick
frequencies over up to the most recent 1,000 valid tick points. They are not
official Deriv Digit Analytics percentages, are not predictions, and do not
establish that a digit is more likely on the next tick.

## Source contract

- Source identifier: `tradebridge_backend_tick_frequency`.
- Market: the payload's `symbol`, which must match the active strategy market.
- Window: `windows["1000"]`, with at least 2 and at most 1,000 valid observed ticks.
- Percentages: all ten digits, finite numeric values from 0 through 100.
- Observation timestamp: backend `calculated_at`, generated when a live tick is processed.
- Event identity: market symbol + latest observed tick epoch + calculation timestamp + window.
- Receipt timestamp: assigned by the receiving process; it must be timezone-aware.
- Health validation: normalized snapshots must pass `PercentageFeedHealth` for source,
  market, freshness, timestamp ordering, duplicate IDs, and complete percentages.

The adapter rejects malformed data and does not synthesize percentage updates.
The live WebSocket bridge ignores historical responses for strategy observations:
history may seed the frequency calculator, but the 10-second observation window
starts at the user's explicit Start command and uses only subsequent live tick
events. A transport disconnect blocks the session; a new connection requires a
fresh Start and a rebuilt observation window.

The live bridge accepts these messages on `/deriv/live/market`:

```json
{"strategy":{"action":"start","symbol":"1HZ100V"}}
{"strategy":{"action":"status"}}
{"strategy":{"action":"stop"}}
```

The response type is `strategy_status`. It reports state, reason, selected
group, trigger digit, feed state, observation count, qualification summary, and
`execution_enabled: false`. The start command also requests 1,000 points of
history to seed the frequency calculator and subscribes to the selected market's
live ticks. It does not place a contract.

Set `TRADEBRIDGE_STRATEGY_FEED_MAX_AGE_SECONDS` to a positive finite value
appropriate for the observed market update cadence (default: 5 seconds). If
source freshness, timestamp order, event identity, market, or data integrity
fails validation, the session blocks rather than using suspect data.

## Tests

```powershell
python -m pytest tests/test_market_frequency_adapter.py tests/test_feed_health.py tests/test_strategy_session.py tests/test_live_strategy_feed.py -v
```

This integration does not add trade execution. Live safety-breach rules,
purchase/settlement reconciliation, and tie handling remain separate work.
Real-account execution remains disabled.
