# Digit Analytics Percentage Data Source

## Status

**Integration blocked pending source verification.** TradeBridge must not label locally computed frequencies as percentages received directly from Deriv's Digit Analytics interface.

## What the repository currently does

- `app/collector.py` subscribes to the Deriv `ticks` stream and stores each tick's quote, precision, epoch and last digit in CSV.
- `app/analysis.py` calculates descriptive digit frequencies from a supplied list of last digits.
- `app/main.py` exposes digit analysis endpoints, including analysis of stored ticks from Supabase.

These are tick-derived statistics. They are not a dedicated Digit Analytics feed.

## Official documentation reviewed

Deriv's published market-data documentation identifies these public endpoints:
- `ticks`: subscribe to live ticks for a symbol.
- `ticks_history`: retrieve historical ticks for a symbol.
- `active_symbols`, `contracts_for`, and related market-data calls.

The published market-data endpoint catalogue and API schema repository reviewed here do not document a dedicated endpoint or response field for streaming the exact percentages displayed in the Digit Analytics interface. Therefore, **no supported direct source has been verified yet**. This is a statement about the public documentation reviewed, not proof that Deriv has no internal, private, or future interface.

References:
- https://developers.deriv.com/docs/data/
- https://developers.deriv.com/docs/data/ticks/
- https://developers.deriv.com/docs/data/ticks-history/
- https://github.com/deriv-com/deriv-api-schemas

For authoritative confirmation, ask Deriv API support (api-support@deriv.com) whether the displayed Digit Analytics per-digit percentages are exposed through a supported public API, what endpoint/message fields provide them, and what the percentage window/update/rounding semantics are. Do not treat an undocumented browser-internal request as a supported integration contract.

## Implementation options — do not proceed without approval

### Option A — derive percentages from live ticks

Maintain a bounded, ordered window of actual last digits. For a window of N valid ticks:

`percentage(d) = 100 * count(d in window) / N`

This produces a **TradeBridge tick-derived percentage**, not necessarily the same value shown in Deriv's interface. The user has explicitly not approved this substitution, so do not implement it as the strategy's percentage source unless they later approve it.

### Option B — ingest percentages from a verified source

If Deriv confirms a supported source, implement an adapter that validates all ten digit values, records source timestamps, rejects stale/incomplete observations, and forwards verified observations to the strategy engine. Document the exact endpoint, payload schema, sampling window, update timing and rounding rules before integration.

## Required decisions before wiring the strategy engine

1. Obtain confirmation from Deriv API support of whether the exact displayed percentages are available through a supported API.
2. If available, identify the documented endpoint/message, fields, timestamp semantics, sample window, refresh rate and rounding behaviour.
3. Define a freshness timeout; stale observations must fail closed.
4. Define how displayed rounded values should be treated at exact threshold boundaries.
5. If Deriv confirms there is no supported feed, ask the user whether to approve an explicitly labelled TradeBridge-derived percentage source. Do not silently substitute it.

## Safety and correctness constraints

- A percentage update and a market tick are separate events.
- Do not manufacture percentage updates when no percentage source update was received.
- Do not treat missing, stale, duplicate, malformed, or incomplete data as stable.
- Preserve timestamps and source metadata for each percentage observation.
- Keep the strategy engine disconnected from trade execution; do not enable demo or real-account trade execution as part of this source-verification step.
- A digit frequency is descriptive and does not establish a predictive edge or guarantee profitability.
