# Digit Analytics Percentage Data Source

## Status

**Integration blocked pending an explicit source/window decision.** The strategy must not label locally computed frequencies as percentages received directly from Deriv's Digit Analytics interface.

## What the repository currently does

- `app/collector.py` subscribes to the Deriv `ticks` stream and stores each tick's quote, precision, epoch and last digit in CSV.
- `app/analysis.py` calculates descriptive digit frequencies from a supplied list of last digits.
- `app/main.py` exposes digit analysis endpoints, including analysis of stored ticks from Supabase.

These are tick-derived statistics. They are not a dedicated Digit Analytics feed.

## Documented public Deriv market-data interfaces checked

Deriv's public API documentation describes:
- `ticks`: subscribe to live ticks for a symbol.
- `ticks_history`: retrieve historical ticks for a symbol.

In the public market-data documentation reviewed for this implementation, no dedicated endpoint was identified that streams the exact percentage values shown in the Digit Analytics UI. This is a finding from the documentation reviewed, not a guarantee that no private, internal, or future interface exists.

References:
- https://developers.deriv.com/docs/data/ticks/
- https://developers.deriv.com/docs/data/ticks-history/
- https://developers.deriv.com/docs/data/

## Implementation options

### Option A — derive percentages from live ticks

Maintain a bounded, ordered window of actual last digits. For a window of N valid ticks:

`percentage(d) = 100 * count(d in window) / N`

This is transparent and reproducible, but the result is a **TradeBridge tick-derived percentage**. To reproduce the UI's values, the sample size, update timing, rounding, reset behaviour and precision handling must first be verified against the UI.

### Option B — ingest percentages from a verified source

If a supported, documented source of Digit Analytics percentages is identified, create an adapter that validates all ten digit values, records timestamps, rejects stale/incomplete observations, and forwards them to the strategy engine. Do not scrape undocumented private endpoints or assume browser-internal APIs are stable.

## Required decisions before wiring the strategy engine

1. Should TradeBridge calculate percentages from its own live tick window, or must it match values shown by Deriv's Digit Analytics interface?
2. If calculating locally, what tick-window size and update policy should be used?
3. What freshness timeout should invalidate percentage observations?
4. How should rounded displayed values be handled at exact threshold boundaries?

## Safety and correctness constraints

- A percentage update and a market tick are separate events.
- Do not manufacture percentage updates when no tick arrives.
- Do not treat missing, stale, duplicate, malformed, or incomplete data as stable.
- Preserve timestamps and the exact tick window used for each calculated percentage.
- Keep the strategy engine disconnected from trade execution until the data source and remaining strategy decisions are explicitly resolved.
- A derived digit frequency is descriptive; it does not establish a predictive edge or guarantee profitability.
