"""Per-WebSocket bridge from live market events into the strategy session.

Observation-only: consumes TradeBridge-calculated digit-frequency snapshots and
actual live ticks, generates strategy signals, and never submits contracts.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Mapping

from app.market_analysis import extract_last_digit
from app.strategy.digit_strategy import DEFAULT_BARRIER, STABILITY_SECONDS, Group, validate_barrier
from app.strategy.feed_health import FeedState, PercentageFeedHealth
from app.strategy.market_frequency_adapter import SOURCE_ID, to_percentage_snapshot
from app.strategy.session import DigitStrategySession, SessionState


class LiveStrategyFeed:
    """One isolated strategy session bound to one market WebSocket connection."""

    def __init__(self) -> None:
        self.symbol: str | None = None
        self.window_size: int = 1000
        self.barrier: int = DEFAULT_BARRIER
        self.session: DigitStrategySession | None = None
        self.feed_health: PercentageFeedHealth | None = None

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def _max_age_seconds() -> float:
        raw = os.getenv("TRADEBRIDGE_STRATEGY_FEED_MAX_AGE_SECONDS", "5")
        try:
            value = float(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("TRADEBRIDGE_STRATEGY_FEED_MAX_AGE_SECONDS must be a positive number.") from exc
        if value <= 0 or value != value or value in (float("inf"), float("-inf")):
            raise ValueError("TRADEBRIDGE_STRATEGY_FEED_MAX_AGE_SECONDS must be a positive finite number.")
        return value

    def start(
        self,
        symbol: str,
        window_size: int = 1000,
        barrier: int = DEFAULT_BARRIER,
        direction: str | None = None,
        initial_frequency: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not isinstance(symbol, str) or not symbol.strip():
            raise ValueError("A valid market symbol is required to start strategy observation.")
        if type(window_size) is not int or window_size not in (50, 100, 250, 500, 1000):
            raise ValueError("Strategy window_size must be one of 50, 100, 250, 500, or 1000 ticks.")
        if not validate_barrier(barrier):
            raise ValueError("Strategy barrier must be an integer digit from 0 to 9.")
        if direction is not None and direction not in ("over", "under"):
            raise ValueError("Strategy direction must be 'over' or 'under'.")
        now = self._now()
        self.symbol = symbol
        self.window_size = window_size
        self.barrier = barrier
        self.feed_health = PercentageFeedHealth(
            expected_source_id=SOURCE_ID,
            expected_market_id=symbol,
            max_age_seconds=self._max_age_seconds(),
        )
        requested_group = Group(direction) if direction is not None else None
        self.session = DigitStrategySession(feed_health=self.feed_health, barrier=barrier, requested_group=requested_group, window_seconds=STABILITY_SECONDS)
        initial_percentages: dict[int, float] | None = None
        initial_observed_at: datetime | None = None
        if isinstance(initial_frequency, Mapping) and initial_frequency.get("symbol") == symbol:
            windows = initial_frequency.get("windows")
            summary = windows.get(str(window_size)) if isinstance(windows, Mapping) else None
            raw_percentages = summary.get("percentages") if isinstance(summary, Mapping) else None
            latest_epoch = initial_frequency.get("latest_tick_epoch")
            try:
                epoch = float(latest_epoch)
                candidate_observed_at = datetime.fromtimestamp(epoch, tz=timezone.utc)
                if isinstance(raw_percentages, Mapping):
                    parsed = {digit: float(raw_percentages[str(digit)]) for digit in range(10)}
                    if candidate_observed_at <= now and now - candidate_observed_at <= self.feed_health.max_age:
                        initial_percentages = parsed
                        initial_observed_at = candidate_observed_at
            except (TypeError, ValueError, OverflowError, OSError, KeyError):
                initial_percentages = None
                initial_observed_at = None
        self.session.start(now, initial_percentages, initial_observed_at)

        reason = (
            "Strategy started in observation-only mode. Waiting for a fresh "
            "TradeBridge backend frequency snapshot for the selected market "
            f"and {self.window_size}-tick window; selected direction is {direction or 'legacy-auto'}, barrier is {self.barrier}; Red/Green identities must remain unchanged "
            f"for {STABILITY_SECONDS:.0f} seconds. These are observed-tick frequencies, not "
            "official Deriv Digit Analytics percentages or predictions."
        )
        return self._status(reason)

    def stop(self) -> dict[str, Any]:
        if self.session is None:
            return self._status("No strategy session is active.")
        update = self.session.stop()
        return self._status(update.reason)

    def on_frequency(self, payload: Mapping[str, Any]) -> dict[str, Any] | None:
        """Consume one real frequency event while observing or monitoring a setup."""
        if self.session is None or self.feed_health is None or self.symbol is None:
            return None
        if self.session.state in (SessionState.BLOCKED, SessionState.STOPPED, SessionState.IDLE):
            return None
        # Continue consuming real frequency snapshots after profile selection:
        # Red/Green, colour positions and thresholds must remain valid until signal.
        if not isinstance(payload, Mapping) or payload.get("symbol") != self.symbol:
            return None
        windows = payload.get("windows")
        summary = windows.get(str(self.window_size)) if isinstance(windows, Mapping) else None
        sample_size = summary.get("sample_size") if isinstance(summary, Mapping) else None
        # Early history can seed the calculator but cannot count as a percentage update.
        if type(sample_size) is int and sample_size < 2:
            return None
        try:
            snapshot = to_percentage_snapshot(payload, received_at=self._now(), window=str(self.window_size))
        except ValueError as exc:
            self.feed_health.mark_disconnected(f"Invalid percentage source event: {exc}")
            self.session.state = SessionState.BLOCKED
            return self._status(f"Strategy blocked: invalid percentage source event ({exc}).")

        if self.session.started_at and snapshot.observed_at < self.session.started_at:
            return None

        update = self.session.add_snapshot(snapshot, self._now())
        if update.state in (SessionState.OBSERVING, SessionState.WAITING_FOR_MORE_DATA):
            update = self.session.evaluate(self._now())
        return self._status(update.reason, update.qualification)

    def on_tick(self, symbol: str, tick: Mapping[str, Any], fallback_pip_size: Any = None) -> dict[str, Any] | None:
        if self.session is None or self.symbol != symbol or self.session.state is not SessionState.SELECTED:
            return None
        pip_size = tick.get("pip_size", fallback_pip_size)
        digit = extract_last_digit(tick.get("quote"), pip_size)
        if digit is None:
            self.session.state = SessionState.BLOCKED
            if self.feed_health:
                self.feed_health.mark_disconnected("Could not safely extract a last digit from a live tick.")
            return self._status("Strategy blocked: could not safely extract the live tick's last digit.")
        update = self.session.on_tick(digit, now=self._now())
        return self._status(update.reason, update.qualification)

    def reject_tick(self, reason: str) -> dict[str, Any] | None:
        """Fail closed when a live tick is invalid, duplicate, or out of order."""
        if self.feed_health is not None:
            self.feed_health.mark_disconnected(reason)
        if self.session is not None and self.session.state not in (SessionState.IDLE, SessionState.STOPPED):
            self.session.state = SessionState.BLOCKED
        if self.session is None:
            return None
        return self._status(f"Strategy blocked: {reason}")

    def disconnected(self, reason: str = "Live market transport disconnected.") -> dict[str, Any] | None:
        if self.feed_health is not None:
            self.feed_health.mark_disconnected(reason)
        if self.session is not None and self.session.state not in (SessionState.IDLE, SessionState.STOPPED):
            self.session.state = SessionState.BLOCKED
        if self.session is None:
            return None
        return self._status(reason)

    def _status(self, reason: str, qualification: Any = None) -> dict[str, Any]:
        session = self.session
        colors = session.bar_colors if session else None
        return {
            "type": "strategy_status",
            "symbol": self.symbol,
            "state": session.state.value if session else SessionState.IDLE.value,
            "reason": reason,
            "selected_group": session.selected_group.value if session and session.selected_group else None,
            "requested_direction": session.requested_group.value if session and session.requested_group else None,
            "barrier": self.barrier,
            "trigger_digit": session.trigger_digit if session else None,
            "trigger_baseline_percentage": session.percentage_baseline if session else None,
            "percentage_triggered": session.percentage_triggered if session else False,
            "signal_generated": session.signal_generated if session else False,
            "stability_seconds_required": session.window_seconds if session else STABILITY_SECONDS,
            "stable_since": session.stable_since.isoformat() if session and session.stable_since else None,
            "red_digits": sorted(colors.red) if colors else [],
            "yellow_digits": sorted(colors.yellow) if colors else [],
            "blue_digits": sorted(colors.blue) if colors else [],
            "green_digits": sorted(colors.green) if colors else [],
            "feed_state": self.feed_health.state.value if self.feed_health else FeedState.DISCONNECTED.value,
            "observation_count": len(session.observations) if session else 0,
            "window_seconds": session.window_seconds if session else STABILITY_SECONDS,
            "digit_window_size": self.window_size,
            "qualification": self._qualification_summary(qualification or (session.qualification if session else None)),
            "execution_enabled": False,
        }

    @staticmethod
    def _qualification_summary(result: Any) -> dict[str, Any] | None:
        if result is None:
            return None
        return {
            "decision": result.decision.value,
            "reason": result.reason,
            "under_qualifies": result.under.qualifies,
            "over_qualifies": result.over.qualifies,
            "under_threshold_passes": result.under.threshold_passes,
            "under_stability_passes": result.under.stability_passes,
            "under_color_positions_pass": result.under.color_positions_pass,
            "under_trigger_passes": result.under.trigger_passes,
            "over_threshold_passes": result.over.threshold_passes,
            "over_stability_passes": result.over.stability_passes,
            "over_color_positions_pass": result.over.color_positions_pass,
            "over_trigger_passes": result.over.trigger_passes,
            "trigger_digit": result.trigger_digit,
        }
