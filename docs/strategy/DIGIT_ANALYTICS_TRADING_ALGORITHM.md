# Deprecated strategy note

This older algorithm description conflicts with the user's latest explicit strategy rules and must not be treated as authoritative. The sole source of truth is [STRATEGY_SOURCE_OF_TRUTH.md](STRATEGY_SOURCE_OF_TRUTH.md). In particular, the user selects Over or Under; qualification alone is not an entry signal; the entry signal requires the percentage trigger and digit sequence; and a completed signal allows at most three sequential buys with a stop after two consecutive losses. A completed sequence stops until the user clicks Run again.

Do not extend or reinterpret unspecified barrier rules based on this historical document.

The backend consumes TradeBridge-calculated frequencies from
`/deriv/live/market`. These are observed-tick frequencies, not official Deriv
Digit Analytics percentages or predictions. The strategy generates signals
only; it does not submit contracts.

## Selected barrier and digit filters

Let the selected barrier be `B` (an integer from 0 to 9):

- **Over B candidate:** evaluate percentages for every digit from 0 through B,
  inclusive. These digits are at or below the barrier.
- **Under B candidate:** evaluate percentages for every digit from B through 9,
  inclusive. These digits are at or above the barrier.
- The selector automatically evaluates both directions at the same B. Over 9
  is not eligible because no last digit can be above 9; Under 0 is not eligible
  because no last digit can be below 0. At B=0 only Over is eligible; at B=9
  only Under is eligible.
- A barrier change starts a fresh stability/strategy session. It must not reuse
  an old candidate, trigger baseline, or entry sequence.

Over 3 and Under 6 remain the examples from the original specification:
Over 3 filters digits 0–3; Under 6 filters digits 6–9.

## Colour ranking and positions

Rank all ten digit percentages together, not just the barrier-filter set:

- Green: highest observed frequency.
- Blue: second-highest distinct frequency level.
- Red: lowest observed frequency.
- Yellow: second-lowest distinct frequency level.

Ties must be preserved; never break them by digit order. If multiple digits tie
for the lowest frequency, every tied digit is Red and entry is blocked until
exactly one Red digit remains.

The documented examples define the colour-zone boundary relative to the
selected barrier:

| Direction candidate | Red and Yellow | Green and Blue |
|---|---|---|
| Over B | Digits 0 through B+1 | Digits B+2 through 9 |
| Under B | Digits B−1 through 9 | Digits 0 through B−2 |

This reproduces Over 3 (Red/Yellow 0–4; Green/Blue 5–9) and Under 6
(Red/Yellow 5–9; Green/Blue 0–4). The zones move when the selected barrier
changes. A direction whose colour zone is too small to contain the required
distinct ranks fails qualification; the other eligible direction is still
evaluated automatically.

For all Over candidates, Red and Yellow must both be strictly below 9%.
Under candidates do not have this extra Red/Yellow percentage threshold.

## Percentage threshold

Apply the filter to every digit in that candidate's barrier range. Qualify when
either:
- every selected digit is strictly below 9.7%; or
- at most one selected digit is at or above 9.7%, and that one digit is no
  greater than 10.5%.

This reproduces the original four-digit rule for Over 3 and Under 6 while
extending its explicitly stated “only one digit may be at or above 9.7%” rule
to a selected barrier range of a different size.

## Fifteen-second Red/Green stability

Do not start the timer until a valid snapshot includes all ten digit
percentages and exactly one Red digit. Red and Green identities must remain
unchanged continuously for 15 seconds. A change in either identity, a tie at
the lowest frequency, or a source-event gap longer than the feed freshness
limit resets the timer. Percentage values may fluctuate: this check is about
rank identities, not max/min percentage spread.

Every real snapshot is validated for source, active market, freshness,
increasing source timestamps, unique event identity, and valid percentages.
Reconnect or barrier change requires a fresh stability interval.

## Percentage trigger and tick sequence

After a sole candidate qualifies, record the lowest-percentage digit inside
that candidate's barrier-filter set as the trigger digit and record its current
percentage as the baseline. If that selected digit is tied for the lowest
percentage in the filter set, do not choose a trigger.

The percentage trigger is a rise of at least 0.1 percentage points from the
recorded baseline in a subsequent frequency snapshot. The trigger digit does
not have to appear as an actual tick. Ignore the tick that produced the
frequency snapshot which detected the rise; only later tick events can count.

After the percentage trigger:
- **Over B:** require at least two consecutive activity ticks with digits 0–B,
  followed by a confirmation digit in B+3…9 (the Over 3 example is 6–9).
- **Under B:** require at least two consecutive activity ticks with digits B–9,
  followed by a confirmation digit in 0…B−3 (the Under 6 example is 0–3).

Digits outside the activity or confirmation sets break the consecutive
sequence. For the original examples this yields Over 3 activity 0–3 followed
by confirmation 6–9, and Under 6 activity 6–9 followed by confirmation 0–3.
The colour zones and confirmation bands narrow or widen with the selected
barrier; confirmation must remain on the contract's opposite side. Barriers whose conditions cannot
produce a valid sequence remain observation-only and cannot submit a signal.

If Red/Green identities, colour positions, or percentage filters become
invalid before the signal, cancel the pending setup and return to observation.

## Signal, execution, and risk

A completed sequence generates an **entry signal only**. Execution remains
disabled. The maximum-three-trades-per-cycle limit, settlement-aware
two-consecutive-loss stop, and purchase/settlement reconciliation must be
implemented and validated in the execution/risk layer before any trading is
enabled.

## Selected data window and tests

The strategy uses the user-selected 50, 100, 250, 500, or 1,000 observed-tick
window. Percentages must agree with the backend-calculated counts and sample
size. These are not official Deriv Digit Analytics percentages.

Run:

```powershell
python -m pytest tests/test_digit_strategy.py tests/test_strategy_session.py tests/test_market_frequency_adapter.py tests/test_feed_health.py tests/test_live_strategy_feed.py -v
```
