import unittest
from datetime import datetime, timedelta, timezone

from app.strategy.digit_strategy import Group
from app.strategy.feed_health import PercentageFeedHealth, PercentageSnapshot
from app.strategy.session import DigitStrategySession, SessionState


class DigitStrategySessionTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
        self.monitor = PercentageFeedHealth("verified-feed-v1", "R_100", 20.0)
        self.session = DigitStrategySession(self.monitor)
        self.percentages = {
            0: 7.0, 1: 8.0, 2: 8.1, 3: 8.2, 4: 8.3,
            5: 10.5, 6: 8.0, 7: 8.0, 8: 8.0, 9: 8.0,
        }

    def snapshot(self, observed_seconds, event_id):
        return self.snapshot_with(observed_seconds, event_id, self.percentages)

    def snapshot_with(self, observed_seconds, event_id, percentages):
        observed = self.start + timedelta(seconds=observed_seconds)
        return PercentageSnapshot(
            "verified-feed-v1", "R_100", observed, observed + timedelta(milliseconds=100),
            percentages, event_id,
        )

    def test_start_resets_all_session_local_state(self):
        self.session.start(self.start)
        self.session.selected_group = Group.UNDER
        self.session.trigger_digit = 0
        self.session.start(self.start + timedelta(minutes=1))
        self.assertEqual(self.session.state, SessionState.OBSERVING)
        self.assertEqual(self.session.observations, [])
        self.assertIsNone(self.session.selected_group)
        self.assertIsNone(self.session.trigger_digit)
        self.assertIsNone(self.session.pattern_tracker)

    def test_valid_initial_window_selects_group_and_tracks_pattern(self):
        self.session.start(self.start)
        self.assertEqual(
            self.session.add_snapshot(self.snapshot(1, "e1"), self.start + timedelta(seconds=2)).state,
            SessionState.OBSERVING,
        )
        self.session.add_snapshot(self.snapshot(9, "e2"), self.start + timedelta(seconds=10))
        result = self.session.evaluate(self.start + timedelta(seconds=10))
        self.assertEqual(result.state, SessionState.SELECTED)
        self.assertEqual(self.session.selected_group, Group.UNDER)
        self.assertEqual(self.session.trigger_digit, 0)
        ticks = [self.session.on_tick(d) for d in [0, 1, 4, 7]]
        self.assertTrue(ticks[-1].pattern_completed)

    def test_incomplete_window_blocks_selection(self):
        self.session.start(self.start)
        self.session.add_snapshot(self.snapshot(1, "e1"), self.start + timedelta(seconds=2))
        result = self.session.evaluate(self.start + timedelta(seconds=10))
        self.assertEqual(result.state, SessionState.BLOCKED)
        self.assertIsNone(self.session.selected_group)

    def test_evaluation_before_window_end_does_not_select(self):
        self.session.start(self.start)
        result = self.session.evaluate(self.start + timedelta(seconds=9))
        self.assertEqual(result.state, SessionState.OBSERVING)
        self.assertIsNone(self.session.selected_group)

    def test_neither_qualifies_then_rolling_window_selects_group(self):
        self.session.start(self.start)
        neither = dict(self.percentages)
        neither.update({0: 10.1, 1: 10.2, 2: 8.1, 3: 8.2, 4: 8.3})
        self.session.add_snapshot(self.snapshot_with(1, "r1", neither), self.start + timedelta(seconds=1, milliseconds=100))
        self.session.add_snapshot(self.snapshot_with(9, "r2", neither), self.start + timedelta(seconds=9, milliseconds=100))
        first = self.session.evaluate(self.start + timedelta(seconds=10))
        self.assertEqual(first.state, SessionState.WAITING_FOR_MORE_DATA)
        self.assertIsNone(self.session.selected_group)

        qualifying = dict(self.percentages)
        self.session.add_snapshot(self.snapshot_with(11, "r3", qualifying), self.start + timedelta(seconds=11, milliseconds=100))
        self.assertEqual(self.session.state, SessionState.WAITING_FOR_MORE_DATA)
        self.session.add_snapshot(self.snapshot_with(19, "r4", qualifying), self.start + timedelta(seconds=19, milliseconds=100))
        result = self.session.add_snapshot(
            self.snapshot_with(21, "r5", qualifying),
            self.start + timedelta(seconds=21, milliseconds=100),
        )
        self.assertEqual(result.state, SessionState.SELECTED)
        self.assertEqual(self.session.selected_group, Group.UNDER)
        self.assertEqual(self.session.trigger_digit, 0)
        self.assertTrue(all(item.observed_at >= self.start + timedelta(seconds=11) for item in self.session.observations))

    def test_rolling_window_does_not_reuse_expired_observations(self):
        self.session.start(self.start)
        neither = dict(self.percentages)
        neither.update({0: 10.1, 1: 10.2})
        self.session.add_snapshot(self.snapshot_with(1, "x1", neither), self.start + timedelta(seconds=1, milliseconds=100))
        self.session.add_snapshot(self.snapshot_with(9, "x2", neither), self.start + timedelta(seconds=9, milliseconds=100))
        self.session.evaluate(self.start + timedelta(seconds=10))
        result = self.session.add_snapshot(
            self.snapshot_with(21, "x3", self.percentages),
            self.start + timedelta(seconds=21, milliseconds=100),
        )
        self.assertEqual(result.state, SessionState.WAITING_FOR_MORE_DATA)
        self.assertEqual(len(self.session.observations), 1)
        self.assertIsNone(self.session.selected_group)

    def test_feed_rejection_blocks_session(self):
        self.session.start(self.start)
        bad = PercentageSnapshot(
            "wrong-source", "R_100", self.start + timedelta(seconds=1),
            self.start + timedelta(seconds=1, milliseconds=100),
            self.percentages, "bad-event",
        )
        result = self.session.add_snapshot(bad, self.start + timedelta(seconds=2))
        self.assertEqual(result.state, SessionState.BLOCKED)
        self.assertIsNone(self.session.selected_group)


if __name__ == "__main__":
    unittest.main()
