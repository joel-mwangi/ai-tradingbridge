# Deprecated strategy note

**Do not implement strategy rules from this document.** Its historical barrier generalizations are not authoritative and may conflict with the approved profiles.

The sole source of truth is [STRATEGY_SOURCE_OF_TRUTH.md](STRATEGY_SOURCE_OF_TRUTH.md).

The backend is approved to qualify and emit strategy signals only for:
- **Over 3**
- **Under 6**

All other direction/barrier combinations are observation-only until a complete profile is explicitly documented and reviewed. Do not extrapolate thresholds, colour zones, percentage triggers, activity sequences, or confirmation bands by shifting an existing profile.

The backend calculates observed-tick frequencies from `/deriv/live/market`; these are not official Deriv Digit Analytics percentages or predictions. The public market socket generates observation-only signals. The authenticated Demo WebSocket may execute one confirmed signal as up to three sequential Demo buys, waiting for settlement and stopping before buy three after two consecutive losses.
