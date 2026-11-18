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
- `GET /even-odd/history`: returns a bounded historical tick snapshot, optionally filtered by epoch range or collection ID.
- `GET /even-odd/quality`: audits quote-digit consistency, parity consistency, symbol consistency, ordering and exact repeated-observation fingerprints without deleting rows.
- `POST /even-odd/collect`: creates a durable collection-run record, collects
  a bounded live tick sample from the selected Deriv symbol, persists normalized
  ticks to the dedicated Even/Odd Supabase project, and returns descriptive
  parity/frequency/sequence/transition summaries.

Collection bounds: 10–300 ticks and 5–120 seconds. A successful collection
must contain the full requested sample. If the upstream feed fails after
receiving valid ticks, those ticks are persisted and the run is marked
`partial`; if no valid ticks arrive, the run is marked `failed`. Both error
responses include the collection ID and received-tick count when available.
The Deriv public WebSocket endpoint is
`wss://api.derivws.com/trading/v1/options/ws/public`. The Even/Odd module owns
a separate connection and lifecycle.

## Tick normalization

- Preserve quote precision using `Decimal`, not binary floating-point rounding.
- Derive the last digit from the formatted quote at the provider's `pip_size`.
- Validate symbol, quote, epoch and precision.
- Derive parity from the normalized last digit: even digits are 0, 2, 4, 6, 8;
  odd digits are 1, 3, 5, 7, 9.
- Deriv's `tick.id` is stored as `deriv_stream_id`: it identifies a
  subscription stream and is not a unique tick identifier.

## Analysis

The engine returns full-sample and available 50/100/250/500/1000-tick rolling
summaries, including parity counts and percentages, ten-digit distributions,
current and longest parity streaks, and parity transition counts.

These outputs are descriptive features only. They do not estimate the next
tick's probability and do not claim a predictive edge.

## Persistence and lifecycle

The collection-run record is inserted before opening the Deriv WebSocket.
Ticks are inserted with one-based `sequence_no` values and a foreign key to
the run. A completed run records the requested count; a partial run records
the received count and a sanitized failure reason; a failed run records zero
received ticks when no valid sample was obtained. Run terminal updates are
best-effort if the database itself becomes unavailable.

Storage uses only `EVEN_ODD_SUPABASE_URL` and the dedicated secret/service-role
key environment variables. It never falls back to the primary application's
Supabase credentials. The collector is request-scoped and bounded; it is not
a continuously running background worker. The authenticated `/even-odd/history` endpoint reads up to 5,000 rows per
request in chronological order. It captures the maximum row ID before paging
and applies that boundary to every page, so concurrent inserts do not shift
the result set mid-read. When both epoch bounds are supplied, both are applied.
Overlapping collection windows can still contain duplicate observations; the
raw history endpoint deliberately preserves source rows rather than silently
deduplicating them. Dataset-level deduplication and quality diagnostics remain
subsequent research-platform work.

## Execution boundary

No order placement, trading credentials, strategy execution, or automatic
signal authorization is exposed by these endpoints.
