"""Side-effect-free state machine for barrier-generalized TradeBridge digit strategies."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum

from app.strategy.digit_strategy import (
    PERCENTAGE_TRIGGER_RISE,
    STABILITY_SECONDS,
    BarColorRanks,
    DEFAULT_BARRIER,
    Decision,
    Group,
    PatternTracker,
    QualificationResult,
    TimestampedPercentageObservation,
    choose_group,
    digits_for,
    rank_bar_colors,
)
from app.strategy.feed_health import FeedHealthResult, PercentageFeedHealth, PercentageSnapshot


class SessionState(str, Enum):
    IDLE = "idle"
    OBSERVING = "observing"
    SELECTED = "selected"
    WAITING_FOR_MORE_DATA = "waiting_for_more_data"
    BLOCKED = "blocked"
    STOPPED = "stopped"


@dataclass(frozen=True)
class SessionUpdate:
    state: SessionState
    reason: str
    qualification: QualificationResult | None = None
    pattern_completed: bool = False
    percentage_triggered: bool = False


@dataclass
class DigitStrategySession:
    """Maintain live ranking stability, automatic profile choice, and signal state.

    This class never places trades. A candidate remains pending only while its
    percentage filter, colour positions, and Red/Green stability still qualify.
    The percentage increase arms a tick-sequence tracker; the trigger digit
    itself does not have to appear as a tick.
    """

    feed_health: PercentageFeedHealth
    barrier: int = DEFAULT_BARRIER
    window_seconds: float = STABILITY_SECONDS
    requested_group: Group | None = None
    state: SessionState = SessionState.IDLE
    started_at: datetime | None = None
    observations: list[TimestampedPercentageObservation] = field(default_factory=list)
    selected_group: Group | None = None
    trigger_digit: int | None = None
    pattern_tracker: PatternTracker | None = None
    qualification: QualificationResult | None = None
    stable_since: datetime | None = None
    stability_passed: bool = False
    bar_colors: BarColorRanks | None = None
    percentage_baseline: float | None = None
    trigger_baselines: dict[int, float] = field(default_factory=dict)
    percentage_triggered: bool = False
    skip_current_tick: bool = False
    signal_generated: bool = False
    execution_locked: bool = False
    _rank_signature: tuple[frozenset[int], frozenset[int]] | None = None
    _last_observed_at: datetime | None = None
    _last_received_at: datetime | None = None

    def __post_init__(self) -> None:
        if type(self.barrier) is not int or self.barrier not in range(10):
            raise ValueError("barrier must be an integer digit from 0 to 9.")
        if (
            isinstance(self.window_seconds, bool)
            or not isinstance(self.window_seconds, (int, float))
            or self.window_seconds <= 0
        ):
            raise ValueError("window_seconds must be positive.")

    def _clear_candidate(self) -> None:
        self.selected_group = None
        self.trigger_digit = None
        self.pattern_tracker = None
        self.percentage_baseline = None
        self.trigger_baselines.clear()
        self.percentage_triggered = False
        self.skip_current_tick = False
        self.signal_generated = False
        self.execution_locked = False

    def lock_execution(self) -> None:
        if self.signal_generated:
            self.execution_locked = True

    def unlock_execution(self) -> None:
        self.execution_locked = False

    def start(
        self,
        started_at: datetime,
        initial_percentages: dict[int, float] | None = None,
        initial_observed_at: datetime | None = None,
    ) -> SessionUpdate:
        """Start a fresh run, anchoring stability to Run only when identities are known.

        If a fresh pre-existing market snapshot is supplied, its Red/Green identities
        are captured at Run and the 15-second clock starts immediately. Without a
        fresh snapshot, identities are unknowable at click time, so the clock starts
        at the first valid snapshot instead of falsely counting unobserved time.
        """
        if not isinstance(started_at, datetime) or started_at.tzinfo is None or started_at.utcoffset() is None:
            raise ValueError("Session start must be timezone-aware.")
        self.started_at = started_at.astimezone(timezone.utc)
        self.observations.clear()
        self._clear_candidate()
        self._rank_signature = None
        self._last_observed_at = None
        self._last_received_at = None
        self.stable_since = None
        self.stability_passed = False
        self.bar_colors = None
        self.qualification = None
        self.state = SessionState.OBSERVING

        initial_is_fresh = (
            isinstance(initial_observed_at, datetime)
            and initial_observed_at.tzinfo is not None
            and initial_observed_at.utcoffset() is not None
            and initial_observed_at.astimezone(timezone.utc) <= self.started_at
            and self.started_at - initial_observed_at.astimezone(timezone.utc) <= self.feed_health.max_age
            and isinstance(initial_percentages, dict)
        )
        if initial_is_fresh:
            try:
                ranks = rank_bar_colors(initial_percentages)
            except (TypeError, ValueError):
                ranks = None
            if ranks is not None and len(ranks.red) == 1:
                self.bar_colors = ranks
                self._rank_signature = ranks.stability_signature
                self.stable_since = self.started_at
                return SessionUpdate(
                    self.state,
                    "Fresh Red/Green identities captured at Run; the 15-second stability timer started.",
                )

        return SessionUpdate(
            self.state,
            "Run started, but no fresh initial Red/Green snapshot was available. The 15-second timer will start at the first valid snapshot; unobserved time will not count.",
        )

    def add_snapshot(self, snapshot: PercentageSnapshot, now: datetime) -> SessionUpdate:
        if self.execution_locked:
            return SessionUpdate(
                self.state,
                "Execution in progress; market qualification is locked until the trade sequence completes.",
                self.qualification,
                percentage_triggered=self.percentage_triggered,
            )
        if (
            self.state not in (
                SessionState.OBSERVING,
                SessionState.WAITING_FOR_MORE_DATA,
                SessionState.SELECTED,
            )
            or self.started_at is None
        ):
            return SessionUpdate(self.state, "Session is not accepting observations.")
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            self.state = SessionState.BLOCKED
            return SessionUpdate(self.state, "Current timestamp must be timezone-aware.")

        health: FeedHealthResult = self.feed_health.accept(snapshot, now)
        if not health.accepted or health.snapshot is None:
            self.state = SessionState.BLOCKED
            self._clear_candidate()
            return SessionUpdate(self.state, f"Feed rejected snapshot: {health.reason}")

        observed_at = health.snapshot.observed_at.astimezone(timezone.utc)
        if observed_at < self.started_at:
            self.state = SessionState.BLOCKED
            self._clear_candidate()
            return SessionUpdate(self.state, "Snapshot predates this strategy session.")

        # A source-event gap longer than the configured freshness interval means
        # continuous 15-second stability was not observed; restart the timer.
        if (
            self._last_observed_at is not None
            and observed_at - self._last_observed_at > self.feed_health.max_age
        ):
            if not self.stability_passed:
                self.stable_since = None
                self._rank_signature = None
                self._clear_candidate()

        self.observations.append(
            TimestampedPercentageObservation(observed_at, dict(health.snapshot.percentages))
        )
        self._last_observed_at = observed_at
        self._last_received_at = health.snapshot.received_at.astimezone(timezone.utc)
        # Bound memory use on long-running sessions. Stable identity continuity
        # is checked on every accepted event and retained in stable_since/signature.
        retention = max(self.window_seconds * 2, 60.0)
        cutoff = observed_at - timedelta(seconds=retention)
        self.observations = [item for item in self.observations if item.observed_at >= cutoff]
        return self._progress(now.astimezone(timezone.utc))

    def evaluate(self, now: datetime) -> SessionUpdate:
        """Reassess current state without inventing a new source observation."""
        if self.state not in (
            SessionState.OBSERVING,
            SessionState.WAITING_FOR_MORE_DATA,
            SessionState.SELECTED,
        ) or not self.observations:
            return SessionUpdate(self.state, "Session has no current observation to evaluate.", self.qualification)
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            self.state = SessionState.BLOCKED
            self._clear_candidate()
            return SessionUpdate(self.state, "Evaluation timestamp must be timezone-aware.")
        # Do not let a wall-clock timer substitute for actual live percentage
        # events: _progress requires the latest source event to span 15 seconds.
        if self.state is SessionState.SELECTED:
            return SessionUpdate(
                self.state,
                "Selected setup remains under live monitoring for filter, stability, and entry conditions.",
                self.qualification,
                percentage_triggered=self.percentage_triggered,
            )
        return self._progress(now.astimezone(timezone.utc))

    def _progress(self, now_utc: datetime) -> SessionUpdate:
        latest = self.observations[-1]
        observed_at = latest.observed_at.astimezone(timezone.utc)
        percentages = latest.percentages

        try:
            ranks = rank_bar_colors(percentages)
        except ValueError as exc:
            self.state = SessionState.BLOCKED
            self._clear_candidate()
            return SessionUpdate(self.state, f"Invalid percentage snapshot: {exc}")

        self.bar_colors = ranks
        signature = ranks.stability_signature

        # A Red tie blocks entry and cannot accumulate stability. When exactly
        # one Red returns, its new identity starts a fresh 15-second interval.
        if len(ranks.red) != 1:
            self._rank_signature = signature
            if not self.stability_passed:
                self.stable_since = None
            self._clear_candidate()
            self.state = SessionState.WAITING_FOR_MORE_DATA
            return SessionUpdate(
                self.state,
                "Multiple digits are tied Red. Wait until exactly one Red digit remains; stability has not started.",
                self.qualification,
            )

        if signature != self._rank_signature:
            prior_signature = self._rank_signature
            self._rank_signature = signature
            if self.stability_passed:
                self.bar_colors = ranks
            else:
                self.stable_since = observed_at
                self._clear_candidate()
                self.state = SessionState.WAITING_FOR_MORE_DATA
                reason = (
                    "First valid Red/Green ranking observed; 15-second stability timer started."
                    if prior_signature is None
                    else "Red or Green identity changed; pending setup cancelled and 15-second timer restarted."
                )
                return SessionUpdate(self.state, reason, self.qualification)

        if self.stable_since is None:
            self.stable_since = observed_at
            self.state = SessionState.WAITING_FOR_MORE_DATA
            return SessionUpdate(self.state, "Red/Green ranking restored; 15-second stability timer started.")

        elapsed = max(0.0, (observed_at - self.stable_since).total_seconds())
        if elapsed < self.window_seconds and not self.stability_passed:
            self.state = SessionState.WAITING_FOR_MORE_DATA
            remaining = self.window_seconds - elapsed
            return SessionUpdate(
                self.state,
                f"Red and Green identities unchanged for {elapsed:.1f}/{self.window_seconds:.0f} seconds; {remaining:.1f} seconds remain.",
                self.qualification,
            )

        if not self.stability_passed:
            self.stability_passed = True
        stable_rows = [
            item.percentages for item in self.observations
            if item.observed_at.astimezone(timezone.utc) >= self.stable_since
        ]
        if len(stable_rows) < 2:
            self.state = SessionState.WAITING_FOR_MORE_DATA
            return SessionUpdate(
                self.state,
                "Waiting for at least two real frequency snapshots within the stable ranking interval.",
                self.qualification,
            )

        current_result = choose_group(
            percentages, stable_rows, self.barrier, stability_passed=self.stability_passed
        )
        if self.requested_group is not None:
            requested_evaluation = current_result.under if self.requested_group is Group.UNDER else current_result.over
            if requested_evaluation.qualifies:
                requested_decision = Decision.SELECT_UNDER if self.requested_group is Group.UNDER else Decision.SELECT_OVER
                current_result = QualificationResult(
                    requested_decision, current_result.under, current_result.over,
                    trigger_digit=(self._requested_trigger_digits(percentages, self.requested_group) or (None,))[0],
                    reason=f"User-selected {self.requested_group.value.title()} {self.barrier} qualifies.",
                )
            else:
                current_result = QualificationResult(
                    Decision.OBSERVE, current_result.under, current_result.over,
                    reason=f"User-selected {self.requested_group.value.title()} {self.barrier} does not qualify; the other direction will not be substituted.",
                )
        self.qualification = current_result

        if self.selected_group is not None:
            still_qualifies = (
                current_result.decision is Decision.SELECT_UNDER and self.selected_group is Group.UNDER
            ) or (
                current_result.decision is Decision.SELECT_OVER and self.selected_group is Group.OVER
            )
            if not still_qualifies:
                # Filter/colour invalidation cancels the candidate. The existing
                # Red/Green timer remains valid if its identities did not change.
                self._clear_candidate()
                self.state = SessionState.WAITING_FOR_MORE_DATA
            else:
                if not self.percentage_triggered and self.trigger_baselines:
                    risen_digits = [
                        digit for digit, baseline in self.trigger_baselines.items()
                        if abs(float(percentages[digit]) - baseline - PERCENTAGE_TRIGGER_RISE) <= 1e-9
                    ]
                    if risen_digits:
                        # If multiple candidates rise by exactly 0.1, use the
                        # candidate with the lowest baseline percentage.
                        self.trigger_digit = min(
                            risen_digits,
                            key=lambda digit: (self.trigger_baselines[digit], digit),
                        )
                        self.percentage_baseline = self.trigger_baselines[self.trigger_digit]
                        self.percentage_triggered = True
                        if self.pattern_tracker is not None:
                            self.pattern_tracker.trigger_digit = self.trigger_digit
                            self.pattern_tracker.arm()
                        # The gateway calls on_tick for the same market event
                        # immediately after on_frequency. That event's tick is
                        # not "after" its own percentage trigger.
                        self.skip_current_tick = True
                        return SessionUpdate(
                            self.state,
                            f"Trigger digit {self.trigger_digit} rose by exactly 0.1 percentage points; now waiting for one or more consecutive activity ticks and an opposite-zone confirmation.",
                            self.qualification,
                            percentage_triggered=True,
                        )
                return SessionUpdate(
                    self.state,
                    "Setup qualified; monitoring the trigger percentage and subsequent tick sequence."
                    if not self.percentage_triggered
                    else "Percentage trigger confirmed; waiting for one or more consecutive activity ticks and an opposite-zone confirmation.",
                    self.qualification,
                    percentage_triggered=self.percentage_triggered,
                )

        # A profile can be selected only after actual percentage observations
        # span the full 15 seconds without Red/Green identities changing.
        if current_result.decision is Decision.SELECT_UNDER:
            self._activate_candidate(Group.UNDER, current_result, percentages)
        elif current_result.decision is Decision.SELECT_OVER:
            self._activate_candidate(Group.OVER, current_result, percentages)
        elif current_result.decision is Decision.INVALID_DATA:
            self.state = SessionState.BLOCKED
            return SessionUpdate(self.state, current_result.reason, current_result)
        else:
            self.state = SessionState.WAITING_FOR_MORE_DATA
            return SessionUpdate(
                self.state,
                f"{current_result.reason} Red/Green have been stable for {elapsed:.1f} seconds; continue observing.",
                current_result,
            )

        return SessionUpdate(
            self.state,
            f"{self.selected_group.value.title()} {self.barrier} automatically selected after {elapsed:.1f} seconds of stable Red/Green identities. Baseline recorded; waiting for a 0.1 percentage-point rise.",
            current_result,
        )

    def _requested_trigger_digits(self, percentages: dict[int, float] | object, group: Group) -> tuple[int, ...]:
        """Return trigger candidates derived from the selected direction and barrier.

        Both directions monitor digits at either of the two lowest distinct
        percentage levels within their selected barrier range.
        Preserve ties instead of silently breaking them by digit order.
        """
        selected = digits_for(group, self.barrier)
        values = {digit: float(percentages[digit]) for digit in selected}  # type: ignore[index]
        levels = sorted(set(values.values()))
        candidate_levels = set(levels[:2])
        return tuple(sorted(digit for digit, value in values.items() if value in candidate_levels))

    def _activate_candidate(
        self,
        group: Group,
        qualification: QualificationResult,
        percentages: dict[int, float] | object,
    ) -> None:
        self.selected_group = group
        self.trigger_digit = qualification.trigger_digit
        trigger_digits = self._requested_trigger_digits(percentages, group)
        if self.trigger_digit is None or not trigger_digits:
            self._clear_candidate()
            self.state = SessionState.WAITING_FOR_MORE_DATA
            return
        self.trigger_baselines = {digit: float(percentages[digit]) for digit in trigger_digits}  # type: ignore[index]
        self.percentage_baseline = self.trigger_baselines[self.trigger_digit]
        self.percentage_triggered = False
        self.skip_current_tick = False
        self.signal_generated = False
        self.pattern_tracker = PatternTracker(group, self.trigger_digit, barrier=self.barrier)
        self.state = SessionState.SELECTED

    def on_tick(self, digit: int, now: datetime | None = None) -> SessionUpdate:
        """Consume a tick, optionally checking frequency freshness, and suppress duplicates."""
        if self.state is not SessionState.SELECTED or self.pattern_tracker is None:
            return SessionUpdate(self.state, "Pattern tracking is unavailable until one profile is selected.")
        if now is not None:
            if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
                self.state = SessionState.BLOCKED
                self._clear_candidate()
                return SessionUpdate(self.state, "Tick validation timestamp must be timezone-aware.")
            if not self.observations:
                self.state = SessionState.BLOCKED
                self._clear_candidate()
                return SessionUpdate(self.state, "No accepted frequency observation is available for tick validation.")
            now_utc = now.astimezone(timezone.utc)
            latest_received = self._last_received_at
            if latest_received is None:
                self.state = SessionState.BLOCKED
                self._clear_candidate()
                return SessionUpdate(self.state, "No accepted frequency receipt is available for tick validation.")
            latest_received = latest_received.astimezone(timezone.utc)
            if now_utc < latest_received or now_utc - latest_received > self.feed_health.max_age:
                self.state = SessionState.BLOCKED
                self._clear_candidate()
                return SessionUpdate(self.state, "Frequency feed is stale or timestamp-inconsistent; strategy blocked.")
        if self.signal_generated:
            return SessionUpdate(
                self.state,
                "Entry signal already generated for this setup; duplicate signals are suppressed.",
                self.qualification,
                percentage_triggered=self.percentage_triggered,
            )
        if self.skip_current_tick:
            self.skip_current_tick = False
            return SessionUpdate(
                self.state,
                "Ignored the tick that produced the percentage trigger; only subsequent ticks count.",
                self.qualification,
                percentage_triggered=self.percentage_triggered,
            )
        if not self.percentage_triggered:
            return SessionUpdate(
                self.state,
                "Waiting for the selected trigger digit's percentage to rise by exactly 0.1 percentage points.",
                self.qualification,
            )
        try:
            completed = self.pattern_tracker.on_tick(digit)
        except ValueError as exc:
            self.state = SessionState.BLOCKED
            self._clear_candidate()
            return SessionUpdate(self.state, str(exc), self.qualification)
        if completed:
            self.signal_generated = True
            self.execution_locked = True
            return SessionUpdate(
                self.state,
                "Entry signal generated; no trade was submitted.",
                self.qualification,
                pattern_completed=True,
                percentage_triggered=True,
            )
        return SessionUpdate(
            self.state,
            f"Post-trigger sequence progress: {self.pattern_tracker.progress} consecutive activity tick(s); at least one is required before confirmation.",
            self.qualification,
            percentage_triggered=True,
        )

    def stop(self) -> SessionUpdate:
        self.state = SessionState.STOPPED
        self._clear_candidate()
        return SessionUpdate(self.state, "Session stopped; no new strategy events will be processed.")
