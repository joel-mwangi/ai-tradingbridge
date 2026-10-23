# TradeBridge Digit Strategy — Over 3 and Under 6

This document describes the implemented, observation-only strategy profiles. The
backend consumes TradeBridge-calculated frequencies from
`/deriv/live/market`; these are observed-tick frequencies, not official Deriv
Digit Analytics percentages, not predictions, and not proof of a future outcome.

## Explicit strategy profiles

The automatic selector supports these two named profiles only:

| Rule | Over 3 | Under 6 |
|---|---|---|
| Percentage filter digits | 0, 1, 2, 3 | 6, 7, 8, 9 |
| Red and Yellow colour zone | 0–4 | 5–9 |
| Green and Blue colour zone | 5–9 | 0–4 |
| Extra Red/Yellow threshold | Both strictly below 9% | None |
| Activity digits after percentage trigger | 0–3 | 6–9 |
| Confirmation digits | 6–9 | 0–3 |

These are profile rules, not a generic formula for arbitrary barriers. Do not
silently turn Over 3 into Over 6 or Under 6 into Under 3. A new barrier needs
its own explicit filter, colour-position, and sequence rules.

## Colour ranking

Rank all ten digits by their current percentage within the selected tick window:

- Green: highest frequency.
- Blue: second-highest distinct frequency level.
- Red: lowest frequency.
- Yellow: second-lowest distinct frequency level.

Every digit tied at a rank receives that colour; ranks must not be broken by
digit order. If two or more digits tie for the lowest frequency, entry is
blocked until exactly one Red digit remains.

## Percentage filter

For the four digits listed in a profile, qualify when all four percentages are
strictly below 9.7%, or when exactly three are below 9.7% and the remaining
digit is no greater than 10.5%. More than one digit at or above 9.7%, or any
exception value above 10.5%, fails the filter.

Over 3 additionally requires both the Red and Yellow digits to be strictly
below 9%. Under 6 has no separate Red/Yellow below-9% threshold.

## Fifteen-second Red/Green stability

Do not start stability until a valid snapshot provides all ten percentages and
exactly one Red digit. The identities of the Red and Green digit sets must remain
unchanged continuously for 15 seconds. A change in either set, a Red tie, or a
live-event gap longer than the configured feed freshness limit resets the timer.
The percentage values themselves may change; this check tracks colour identity,
not percentage spread.

The automatic selector chooses Over 3 or Under 6 only when exactly one profile
passes its percentage filter, current colour positions, unique-trigger condition,
and Red/Green stability. If neither or both qualify, the algorithm continues
observing rather than choosing arbitrarily.

## Entry sequence

After profile selection, record the selected trigger digit's current percentage
as its baseline. The trigger is a rise of at least 0.1 percentage points from
that baseline, measured from a new percentage snapshot. The trigger digit does
not have to appear as a tick.

After the percentage trigger, ignore the market tick that produced that
frequency snapshot. Then require at least two consecutive activity digits from
the selected profile's activity zone, followed by a confirmation digit from its
opposite zone:

- Over 3: two or more consecutive digits from 0–3, then a digit from 6–9.
- Under 6: two or more consecutive digits from 6–9, then a digit from 0–3.

Digits 4 and 5 break the activity sequence. If Red/Green identities, colour
positions, or percentage filters become invalid before the signal, cancel the
pending setup and resume observation.

A completed sequence generates an **entry signal only**. The current strategy
does not submit a contract. Contract execution, settlement-aware loss counting,
the maximum-three-trades control, and the two-consecutive-loss stop must be
implemented and validated in the execution/risk layer before trading is enabled.

## Source and freshness contract

- Source identifier: `tradebridge_backend_tick_frequency`.
- Market symbol must match the active session.
- Window: the user's selected 50, 100, 250, 500, or 1,000 valid observed ticks.
- Percentages must agree with the counts and sample size and include all ten digits.
- Source tick timestamps and event identities must be strictly increasing.
- The feed health monitor rejects stale, duplicated, invalid, or out-of-order
  events. A source disconnect blocks the active session.
- A new session must build a new 15-second Red/Green stability interval.

## Tests

Run the strategy tests with:

```powershell
python -m pytest tests/test_digit_strategy.py tests/test_strategy_session.py tests/test_market_frequency_adapter.py tests/test_feed_health.py tests/test_live_strategy_feed.py -v
```
