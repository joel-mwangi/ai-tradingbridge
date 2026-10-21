"""Per-WebSocket bridge from live market events into the strategy session.

This is observation-only. It consumes backend-calculated digit_frequency
snapshots and actual tick quotes, reports state, and never submits contracts.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Mapping

from app.market_analysis import extract_last_digit
from app.strategy.feed_health import FeedState, PercentageFeedHealth
from app.strategy.market_frequency_adapter import SOURCE_ID, STRATEGY_WINDOW, to_percentage_snapshot
from app.strategy.session import DigitStrategySession, SessionState


class LiveStrategyFeed:
    """One isolated strategy session bound to one market WebSocket connection."""

    def __init__(self) -> None:
        self.symbol: str | None = None
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

    def start(self, symbol: str) -> dict[str, Any]:
        if not isinstance(symbol, str) or not symbol.strip():
            raise ValueError("A valid market symbol is required to start strategy observation.")
        now = self._now()
        self.symbol = symbol
        self.feed_health = PercentageFeedHealth(
            expected_source_id=SOURCE_ID,
            expected_market_id=symbol,
            max_age_seconds=self._max_age_seconds(),
        )
        self.session = DigitStrategySession(feed_health=self.feed_health, window_seconds=10.0)
        update = self.session.start(now)
        return self._status(update.reason)

    def stop(self) -> dict[str, Any]:
        if self.session is None:
            return self._status("No strategy session is active.")
        update = self.session.stop()
        return self._status(update.reason)

    def on_frequency(self, payload: Mapping[str, Any]) -> dict[str, Any] | None:
        """Consume one real backend frequency event; ignore insufficient history."""
        if self.session is None or self.feed_health is None or self.symbol is None:
            return None
        if not isinstance(payload, Mapping) or payload.get("symbol") != self.symbol:
            return None
        windows = payload.get("windows")
        summary = windows.get(STRATEGY_WINDOW) if isinstance(windows, Mapping) else None
        # Early history may contain fewer than two valid points. This is not a
        # bad feed event; wait for actual ticks to build a usable histogram.
        sample_size = summary.get("sample_size") if isinstance(summary, Mapping) else None
        if type(sample_size) is int and sample_size < 2:
            return None
        try:
            snapshot = to_percentage_snapshot(payload, received_at=self._now())
        except ValueError as exc:
            self.feed_health.mark_disconnected(f"Invalid percentage source event: {exc}")
            self.session.state = SessionState.BLOCKED
            return self._status(f"Strategy blocked: invalid percentage source event ({exc}).")

        if self.session.started_at and snapshot.observed_at < self.session.started_at:
            # A history response can include points from before Start. It must
            # seed the histogram but must not enter the fresh observation window.
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
        update = self.session.on_tick(digit)
        return self._status(update.reason, update.qualification)

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
        return {
            "type": "strategy_status",
            "symbol": self.symbol,
            "state": session.state.value if session else SessionState.IDLE.value,
            "reason": reason,
            "selected_group": session.selected_group.value if session and session.selected_group else None,
            "trigger_digit": session.trigger_digit if session else None,
            "feed_state": self.feed_health.state.value if self.feed_health else FeedState.DISCONNECTED.value,
            "observation_count": len(session.observations) if session else 0,
            "window_seconds": session.window_seconds if session else 10.0,
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
            "over_threshold_passes": result.over.threshold_passes,
            "over_stability_passes": result.over.stability_passes,
            "trigger_digit": result.trigger_digit,
        }
