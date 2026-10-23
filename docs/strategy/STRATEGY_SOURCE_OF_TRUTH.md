# TradeBridge strategy source of truth

This document is authoritative for the digit strategy. Implementations must not introduce conflicting defaults or infer rules from older documentation.

## Run lifecycle

1. The user chooses **Over or Under**, selects a barrier digit, and clicks **Run**.
2. Run starts a fresh strategy session bound to the selected market and the user's selected frequency window (50, 100, 250, 500, or 1,000 observed ticks).
3. At Run, begin observing the current Red and Green digit identities. Stability passes only if both identities remain unchanged continuously for 15 seconds.
4. If Red or Green changes at any point, cancel the pending setup and restart a fresh 15-second stability window. Do not carry elapsed time, a candidate, a percentage baseline, or an entry sequence across the reset.
5. Use the user's selected tick window for all digit percentages and colour rankings. Colours are recalculated from the ten digit frequencies: Green = most frequent, Blue = second most frequent, Red = least frequent, Yellow = second least frequent. Ties must not be broken by digit order; exactly one Red digit is required.
6. After stability, every applicable qualification must pass: barrier-specific percentage thresholds, colour positions, exactly one Red bar, the percentage-increase trigger, and the required subsequent tick sequence. Qualification alone is not an entry signal.
7. The trigger is an increase of 0.1 percentage point (for example, 8.4% to 8.5%) in the specified trigger digit's frequency percentage. Only ticks after the tick/event that detected the increase count toward the sequence.
8. A valid completed tick sequence creates one entry signal. That signal can start one execution sequence of up to three sequential buys. Wait for each contract to settle and reconcile before submitting the next.
9. Stop the sequence immediately after two consecutive settled losses. A win or breakeven resets the consecutive-loss count. Never submit buy 3 after two consecutive losses. Never submit more than three buys from one signal.
10. When the sequence completes or is stopped by two consecutive losses, stop the strategy session. Do not restart automatically. The user must click Run to create a fresh session and signal.
11. Unknown, rejected, duplicate, or unreconciled contract outcomes must halt execution and require reconciliation; never automatically retry an order with an unknown outcome.
12. Real-account strategy auto-execution remains disabled unless separately reviewed and explicitly enabled. Demo execution must be clearly identified as demo.

## Strategy examples

### Over 3
- Percentage set: digits 0, 1, 2, 3. At least three must be strictly below 9.7%; the remaining digit must be strictly below 10.5%. All four below 9.7% also passes.
- Red and Yellow must be in digits 0–4 and each must be strictly below 9%. Green and Blue must be in digits 5–9.
- Require exactly one Red digit.
- Watch the least- or second-least-appearing digit among 0–3; wait for its percentage to rise by 0.1 percentage point.
- Then require one or more lower digits (0–3), followed by an upper digit (6–9), before the Over 3 signal is confirmed.

### Under 6
- Percentage set: digits 6, 7, 8, 9. At least three must be strictly below 9.7%.
- Red and Yellow must be in digits 5–9. Green and Blue must be in digits 0–4.
- Require exactly one Red digit.
- Watch the least-appearing digit among 6–9; wait for its percentage to rise by 0.1 percentage point.
- Then require one or more upper digits (6–9), followed by a lower digit (0–3), before the Under 6 signal is confirmed.

## Barrier generalization

The selected barrier determines the corresponding digit sets and positions, but the explicit Over 3 and Under 6 examples above must not be altered. Do not silently extrapolate unspecified thresholds to other barriers. If a barrier's complete rules are not defined, fail closed (observation only) rather than inventing rules.

## Verification requirements

Tests must cover: stable 15-second pass; Red/Green change resetting the full timer; selected tick-window propagation; exact strict percentage boundaries; ties for Red; selected direction respected; no execution on qualification alone; exactly one execution sequence per signal; sequential settlement reconciliation; two consecutive losses preventing the next buy; three buys maximum; and requiring a fresh Run after a sequence ends.
