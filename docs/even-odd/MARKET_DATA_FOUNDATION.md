# Even/Odd Lab — Market Data Foundation

## Isolation

The Even/Odd collector creates its own Deriv public WebSocket connection for
each bounded collection request. It does not import or consume the legacy
`app.collector`, strategy live feed, market analysis, MT5 bridge, or trading
session modules. The only shared integration is the existing backend API-token
authentication boundary.

## Endpoints

All endpoints require the existing server-to-server bearer token.

- `GET /even-odd/status`: reports module availability and research-only state.
- `POST /even-odd/analyze`: analyzes a supplied last-digit sample.
- `POST /even-odd/collect`: collects a bounded live tick sample from the
  selected Deriv symbol, normalizes quote precision, and returns tick-level
  data plus parity/frequency/sequence/transition summaries.

Collection bounds: 10–300 ticks and 5–120 seconds. A sample with fewer than
10 valid ticks is rejected. The Deriv public WebSocket endpoint is
`wss://api.derivws.com/trading/v1/options/ws/public`, matching the endpoint
used by the repository's existing standalone collector, but the Even/Odd
module owns a separate connection and lifecycle.

## Tick normalization

- Preserve quote precision using `Decimal`, not binary floating-point rounding.
- Derive the last digit from the formatted quote at the provider's `pip_size`.
- Validate symbol, quote, epoch, precision and tick identifier.
- Derive parity from the normalized last digit: even digits are 0, 2, 4, 6, 8;
  odd digits are 1, 3, 5, 7, 9.

## Analysis

The engine returns full-sample and available 50/100/250/500/1000-tick rolling
summaries, including parity counts and percentages, ten-digit distributions,
current and longest parity streaks, and parity transition counts.

These outputs are descriptive features only. They do not estimate the next
tick's probability and do not claim a predictive edge.

## Persistence and deployment boundary

This milestone returns collected samples in the API response; it does not yet
persist them. This is intentional until a dedicated `even_odd_ticks` schema,
retention policy, deduplication key and service-role access policy are reviewed.
The collector is request-scoped and bounded. It is not a continuously running
background worker; serverless request lifetimes make an unmanaged process-local
collector unreliable. A persistent stream/worker and historical storage are
the next data-platform milestone.

## Execution boundary

No order placement, trading credentials, strategy execution, or automatic
signal authorization is exposed by these endpoints.
