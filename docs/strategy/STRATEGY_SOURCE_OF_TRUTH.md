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

## Generalized barrier formulas

Let `B` be the barrier digit selected in the UI. The same `B` must be passed unchanged through the API, session, qualification engine, pattern tracker, signal, and execution request. Do not substitute a fixed example barrier.

### Over B

- **Percentage set:** digits `0..B` (inclusive), containing `B+1` digits. At least `B` of these digits must be strictly below 9.7%; the one remaining digit, if any, must be strictly below 10.5%. If all selected digits are below 9.7%, the rule passes. This is the generalized interpretation of Over 3 and Over 4.
- **Colour positions:** Red and Yellow must be within digits `0..B+1` and each strictly below 9%; Green and Blue must be within digits `B+2..9`.
- **Red count:** exactly one Red digit. Two or more tied Red digits block qualification.
- **Trigger:** monitor digits in `0..B` at either of the two lowest distinct frequency-percentage levels. A candidate triggers when its percentage rises by at least 0.1 percentage point.
- **Activity sequence:** after the percentage trigger, require one or more ticks in `0..B`, followed by a confirmation tick in `B+3..9`.
- **Valid signal range:** the selected barrier must be valid for Over and the confirmation set `B+3..9` must be non-empty. If the formula cannot produce a confirmation digit, keep the session observation-only.

### Under B

- **Percentage set:** digits `B..9` (inclusive), containing `10-B` digits. At least `(10-B)-1` digits must be strictly below 9.7%. The remaining digit, if any, has no separate 10.5% cap; this follows the Under 6 example.
- **Colour positions:** Red and Yellow must be within digits `B-1..9` and each strictly below 9%; Green and Blue must be within digits `0..B-2`.
- **Red count:** exactly one Red digit. Two or more tied Red digits block qualification.
- **Trigger:** monitor the least-frequency distinct level within digits `B..9`. A candidate triggers when its percentage rises by at least 0.1 percentage point.
- **Activity sequence:** after the percentage trigger, require one or more ticks in `B..9`, followed by a confirmation tick in `0..B-3`.
- **Valid signal range:** the selected barrier must be valid for Under and the confirmation set `0..B-3` must be non-empty. If the formula cannot produce a confirmation digit, keep the session observation-only.

### Examples

- **Over 3:** percentage digits 0–3; Red/Yellow 0–4; Green/Blue 5–9; activity 0–3; confirmation 6–9.
- **Over 4:** percentage digits 0–4; Red/Yellow 0–5; Green/Blue 6–9; activity 0–4; confirmation 7–9.
- **Under 6:** percentage digits 6–9; Red/Yellow 5–9; Green/Blue 0–4; activity 6–9; confirmation 0–3.
- **Under 5:** percentage digits 5–9; Red/Yellow 4–9; Green/Blue 0–3; activity 5–9; confirmation 0–2.

## Stability and execution rules

- **Stability:** beginning at Run, Red and Green digit identities must remain unchanged continuously for 15 seconds. A change resets the full stability period and clears pending trigger/sequence state.
- **Signal:** qualification alone is not an entry signal. The selected direction's percentage trigger and subsequent sequence must complete first.
- **Automatic buys:** one confirmed signal starts up to three sequential buys. Await settlement and reconcile each result before the next buy.
- **Loss protection:** stop immediately after two consecutive settled losses; never submit buy 3 after two losses. A win or breakeven resets the consecutive-loss count.
- **Session lifecycle:** stop after completion or the two-loss limit. Do not restart automatically; the user must click Run again.
- **Safety:** unknown, rejected, duplicate, or unreconciled contract outcomes halt execution for reconciliation. Real-account strategy auto-execution remains disabled unless separately reviewed and explicitly enabled; Demo execution must be clearly identified.
- **Unsupported edges:** valid contract barriers can still have empty generalized confirmation sets. Such combinations must remain observation-only rather than generating impossible signals.

## Verification requirements

Tests must cover: stable 15-second pass; Red/Green change resetting the full timer; selected tick-window propagation; exact strict percentage boundaries; ties for Red; selected direction respected; no execution on qualification alone; exactly one execution sequence per signal; sequential settlement reconciliation; two consecutive losses preventing the next buy; three buys maximum; and requiring a fresh Run after a sequence ends.
