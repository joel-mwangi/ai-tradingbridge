# TradeBridge Digit Strategy

This package contains a rules engine, an observation-only live-feed bridge, and
a sequential Options execution coordinator for authenticated trading sessions.

## Files
- `digit_strategy.py`: thresholds, stability spread, group selection, trigger digit, and pattern recognition.
- `feed_health.py`: fail-closed source/market/freshness/order/duplicate validation.
- `market_frequency_adapter.py`: normalizes backend `digit_frequency` payloads.
- `session.py`: fresh-session lifecycle, initial 15-second Red/Green stability window, rolling reassessment, group selection, and pattern tracking.
- `live_feed.py`: binds actual live percentage snapshots and ticks to one isolated strategy session; reports state only.
- `execution.py`: coordinates up to three sequential Options buys for one confirmed signal; tracks proposal/buy/settlement state and consecutive losses.

## Percentage source

The strategy input is `tradebridge_backend_tick_frequency`, using
`windows["1000"]` from the backend's observed tick histogram. These are
TradeBridge-calculated frequencies from up to the last 1,000 valid observed
tick points, not official Deriv Digit Analytics percentages and not predictions.

The adapter rejects unknown sources, missing symbols/windows, insufficient or
impossible sample sizes, incomplete/non-finite/out-of-range percentages, and
invalid timestamps. Each normalized snapshot uses the provider's latest tick epoch as its observation
timestamp and carries a stable market-scoped event ID derived from that source
tick's timestamp and quote. The server calculation timestamp is diagnostic only.
Live ticks must be strictly time-ordered; duplicate or non-increasing events block
the active strategy session. Every snapshot must
pass `PercentageFeedHealth`; a matching source ID does not independently
authenticate the transport.

## Live market WebSocket control protocol

The `/deriv/live/market` WebSocket accepts the following control messages,
which are handled by TradeBridge and are not forwarded to Deriv:

```json
{"strategy":{"action":"start","symbol":"1HZ100V"}}
{"strategy":{"action":"status"}}
{"strategy":{"action":"stop"}}
```

Start creates a fresh per-connection session, requests recent tick history to
seed the frequency calculator, subscribes to live ticks for the chosen market,
and begins the 15-second Red/Green stability window. Historical seed data is never
counted as a post-Start observation. Only subsequent live tick events with
valid frequency snapshots can advance the strategy.

The WebSocket sends `strategy_status` messages containing state, reason,
selected group, trigger digit, feed state, observation count, qualification
details, and `execution_enabled: false`. States include observing, selected,
waiting_for_more_data, blocked, and stopped. A disconnect blocks the session;
a new connection must start a fresh session and rebuild the full window.

Configure `TRADEBRIDGE_STRATEGY_FEED_MAX_AGE_SECONDS` to a positive finite
value appropriate for the market's update cadence. It defaults to 5 seconds.
Stale, duplicated, out-of-order, malformed, or wrong-market snapshots fail
closed. Never synthesize percentage updates or silently switch source types.

## Tests

```powershell
python -m pytest tests/test_digit_strategy.py tests/test_feed_health.py tests/test_strategy_session.py tests/test_market_frequency_adapter.py tests/test_live_strategy_feed.py -v
```

## Not implemented in this step

- Frontend controls and display for `strategy_status`.
- Live safety-breach predicate.
- Tie-break for equal lowest percentages.
- Real-money execution, purchase acknowledgements, and settlement reconciliation.

Execution is coordinated by `execution.py` and driven through the authenticated `/deriv/ws`
WebSocket proxy. The proxy accepts both Demo and Real Options accounts. Real proposals, buys, and
strategy starts require the server setting `TRADEBRIDGE_REAL_TRADING_ENABLED=true`, a positive
finite `TRADEBRIDGE_REAL_MAX_STAKE`, and the per-connection confirmation message documented in
the root README. The confirmation resets on disconnect; the stake cap is always enforced.
Automated Real runs should remain disabled in production until account-specific risk limits,
operational monitoring, and Real execution tests are independently reviewed.

Real accounts are a required supported mode for API-supported products, not a promise that every
Deriv product is available through this gateway. Multipliers support is not implemented and must
be verified against the intended WebSocket session before integration. Deriv's published MT5 API
does not support MT5 trading; CFD orders through MT5 are outside this package's supported scope.
