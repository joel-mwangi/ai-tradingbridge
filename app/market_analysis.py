"""Backend-owned descriptive digit-frequency snapshots for live market ticks.

These are TradeBridge-calculated frequencies from observed ticks, not official
Deriv Digit Analytics values and not trading signals.
"""
from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass, field
import math
from typing import Any, Iterable

WINDOW_SIZES = (50, 100, 250, 500, 1000)
MAX_POINTS = max(WINDOW_SIZES)


def extract_last_digit(quote: Any, pip_size: Any = None) -> int | None:
    """Extract the final displayed decimal digit without guessing lost precision."""
    if isinstance(quote, bool) or not isinstance(quote, (str, int, float)):
        return None
    try:
        value = float(quote)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(value):
        return None

    raw = str(quote)
    if pip_size is not None:
        if type(pip_size) is not int or not 0 <= pip_size <= 12:
            return None
        precision = pip_size
    else:
        precision = len(raw.split(".", 1)[1]) if isinstance(quote, str) and "." in raw else None
    if precision is None or not 0 <= precision <= 12:
        return None
    formatted = f"{value:.{precision}f}"
    fraction = formatted.split(".", 1)[1] if "." in formatted else ""
    char = fraction[precision - 1] if precision else formatted[-1]
    digit = int(char)
    return digit if 0 <= digit <= 9 else None


@dataclass
class DigitFrequencyWindow:
    """A bounded, per-connection/per-market window of actual observed tick points."""
    points: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=MAX_POINTS))
    # History has second-resolution timestamps but no event IDs. Keep unmatched
    # history fingerprints so the first identical live event is not counted twice.
    _history_overlap_pending: Counter[tuple[float, float, int | None]] = field(default_factory=Counter, repr=False)

    @staticmethod
    def _fallback_event_id(timestamp: float, numeric_price: float) -> str:
        return f"{timestamp:.6f}:{numeric_price:.12g}"

    def seed_history(
        self,
        prices: Iterable[Any],
        times: Iterable[Any],
        pip_size: Any = None,
    ) -> None:
        # Merge history with live ticks that arrived while history was in flight.
        # Since the history response has no provider event IDs, retain one-shot
        # fingerprints for history rows not already represented by live events.
        merged: dict[tuple[float, float, int | None], dict[str, Any]] = {}
        live_counts: Counter[tuple[float, float, int | None]] = Counter()
        for point in self.points:
            key = (point["time"], point["price"], point["last_digit"])
            merged[key] = point
            live_counts[key] += 1
        self._history_overlap_pending.clear()
        for price, epoch in zip(prices, times):
            try:
                timestamp = float(epoch)
                numeric_price = float(price)
            except (TypeError, ValueError, OverflowError):
                continue
            if not math.isfinite(timestamp) or not math.isfinite(numeric_price):
                continue
            point = {
                "time": timestamp,
                "price": numeric_price,
                "last_digit": extract_last_digit(price, pip_size),
                "event_id": self._fallback_event_id(timestamp, numeric_price),
            }
            key = (point["time"], point["price"], point["last_digit"])
            if live_counts[key] > 0:
                live_counts[key] -= 1
                continue
            if key not in merged:
                merged[key] = point
                self._history_overlap_pending[key] += 1
        self.points = deque(
            sorted(merged.values(), key=lambda point: point["time"])[-MAX_POINTS:],
            maxlen=MAX_POINTS,
        )

    def add_tick(
        self,
        quote: Any,
        epoch: Any,
        pip_size: Any = None,
        source_event_id: Any = None,
    ) -> str:
        """Append an ordered live tick.

        Equal provider timestamps are allowed when they identify distinct events:
        epoch fields commonly have only one-second precision. Exact repeated event
        IDs are duplicates; older timestamps are out of order.
        """
        try:
            timestamp = float(epoch)
            numeric_price = float(quote)
        except (TypeError, ValueError, OverflowError):
            return "invalid"
        if not math.isfinite(timestamp) or not math.isfinite(numeric_price):
            return "invalid"
        digit = extract_last_digit(quote, pip_size)
        if digit is None:
            return "invalid"

        if isinstance(source_event_id, (str, int)) and not isinstance(source_event_id, bool) and str(source_event_id).strip():
            event_id = f"provider:{str(source_event_id).strip()}"
        else:
            event_id = self._fallback_event_id(timestamp, numeric_price)

        fingerprint = (timestamp, numeric_price, digit)
        # A history row can legitimately overlap the first live subscription
        # events. Suppress that one-shot overlap without classifying it as a
        # repeated live event; the strategy gateway must not block on this case.
        if self._history_overlap_pending[fingerprint] > 0:
            self._history_overlap_pending[fingerprint] -= 1
            if self._history_overlap_pending[fingerprint] <= 0:
                del self._history_overlap_pending[fingerprint]
            return "history_overlap"

        if any(point.get("event_id") == event_id for point in self.points):
            return "duplicate"

        if self.points and timestamp < self.points[-1]["time"]:
            return "out_of_order"

        point = {
            "time": timestamp,
            "price": numeric_price,
            "last_digit": digit,
            "event_id": event_id,
        }
        self.points.append(point)
        return "accepted"

    def snapshot(self, symbol: str, calculated_at: float | None = None) -> dict[str, Any]:
        points = list(self.points)
        by_window: dict[str, dict[str, Any]] = {}
        for size in WINDOW_SIZES:
            window = points[-size:]
            valid = [p["last_digit"] for p in window if type(p.get("last_digit")) is int and 0 <= p["last_digit"] <= 9]
            counts = Counter(valid)
            total = len(valid)
            by_window[str(size)] = {
                "window_size": size,
                "sample_size": total,
                "counts": {str(digit): counts[digit] for digit in range(10)},
                "percentages": {
                    str(digit): round(counts[digit] / total * 100, 1) if total else None
                    for digit in range(10)
                },
            }
        latest = points[-1] if points else None
        return {
            "source": "tradebridge_backend_tick_frequency",
            "symbol": symbol,
            "calculated_at": calculated_at,
            "latest_tick_epoch": latest["time"] if latest else None,
            "latest_tick_event_id": latest["event_id"] if latest else None,
            "windows": by_window,
            "warning": "Locally calculated observed-tick frequencies; not official Deriv Digit Analytics and not a prediction.",
        }
