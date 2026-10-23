import unittest
from datetime import datetime, timedelta, timezone

from app.strategy.digit_strategy import Group
from app.strategy.feed_health import PercentageFeedHealth, PercentageSnapshot
from app.strategy.session import DigitStrategySession, SessionState


def over3_percentages():
    return {
        0: 8.0, 1: 8.2, 2: 8.5, 3: 9.1, 4: 8.4,
        5: 10.0, 6: 10.1, 7: 10.2, 8: 10.8, 9: 10.9,
    }


def under6_percentages():
    return {
        0: 10.9, 1: 10.8, 2: 10.7, 3: 10.6, 4: 10.5,
        5: 8.5, 6: 8.0, 7: 8.2, 8: 8.3, 9: 8.4,
    }


class DigitStrategySessionTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)
        self.monitor = PercentageFeedHealth("verified-feed-v1", "1HZ100V", 20.0)
        self.session = DigitStrategySession(self.monitor, barrier=3)
        self.session.start(self.start)

    def add(self, seconds, percentages, event_id):
        observed = self.start + timedelta(seconds=seconds)
        snapshot = PercentageSnapshot(
            "verified-feed-v1",
            "1HZ100V",
            observed,
            observed + timedelta(milliseconds=100),
            dict(percentages),
            event_id,
        )
        return self.session.add_snapshot(snapshot, observed + timedelta(milliseconds=200))

    def start_stable_over3(self):
        self.add(1, over3_percentages(), "e1")
        self.add(8, over3_percentages(), "e2")
        return self.add(16, over3_percentages(), "e3")

    def test_start_resets_session_local_state_and_uses_fifteen_seconds(self):
        self.assertEqual(self.session.state, SessionState.OBSERVING)
        self.assertEqual(self.session.window_seconds, 15.0)
        self.add(1, over3_percentages(), "e1")
        self.session.start(self.start + timedelta(minutes=1))
        self.assertEqual(self.session.state, SessionState.OBSERVING)
        self.assertEqual(self.session.observations, [])
        self.assertIsNone(self.session.selected_group)
        self.assertIsNone(self.session.stable_since)
        self.assertIsNone(self.session.pattern_tracker)

    def test_first_valid_snapshot_starts_red_green_timer(self):
        update = self.add(1, over3_percentages(), "e1")
        self.assertEqual(update.state, SessionState.WAITING_FOR_MORE_DATA)
        self.assertEqual(self.session.stable_since, self.start + timedelta(seconds=1))
        self.assertIn("timer started", update.reason)

    def test_selects_over3_only_after_full_fifteen_seconds(self):
        self.add(1, over3_percentages(), "e1")
        self.add(8, over3_percentages(), "e2")
        self.assertEqual(self.session.state, SessionState.WAITING_FOR_MORE_DATA)
        update = self.add(16, over3_percentages(), "e3")
        self.assertEqual(update.state, SessionState.SELECTED)
        self.assertEqual(self.session.selected_group, Group.OVER)
        self.assertEqual(self.session.trigger_digit, 0)
        self.assertEqual(self.session.percentage_baseline, 8.0)
        self.assertFalse(self.session.percentage_triggered)

    def test_selects_under6_automatically_after_stability(self):
        self.session = DigitStrategySession(self.monitor, barrier=6)
        self.session.start(self.start)
        self.add(1, under6_percentages(), "u1")
        self.add(8, under6_percentages(), "u2")
        update = self.add(16, under6_percentages(), "u3")
        self.assertEqual(update.state, SessionState.SELECTED)
        self.assertEqual(self.session.selected_group, Group.UNDER)
        self.assertEqual(self.session.trigger_digit, 6)

    def test_red_tie_blocks_and_does_not_start_timer(self):
        tied = over3_percentages()
        tied[1] = tied[0]
        first = self.add(1, tied, "t1")
        self.assertEqual(first.state, SessionState.WAITING_FOR_MORE_DATA)
        self.assertIsNone(self.session.stable_since)
        self.assertIn("tied Red", first.reason)
        self.add(8, tied, "t2")
        self.assertIsNone(self.session.stable_since)

    def test_green_identity_change_cancels_candidate_and_restarts_timer(self):
        self.add(1, over3_percentages(), "g1")
        changed = dict(over3_percentages())
        changed[5] = 11.0
        self.add(8, changed, "g2")
        self.assertIsNone(self.session.selected_group)
        self.assertEqual(self.session.stable_since, self.start + timedelta(seconds=8))
        update = self.add(16, changed, "g3")
        self.assertEqual(update.state, SessionState.WAITING_FOR_MORE_DATA)
        update = self.add(24, changed, "g4")
        self.assertEqual(update.state, SessionState.SELECTED)
        self.assertEqual(self.session.selected_group, Group.OVER)

    def test_feed_gap_resets_stability(self):
        self.add(1, over3_percentages(), "gap1")
        self.add(8, over3_percentages(), "gap2")
        self.add(31, over3_percentages(), "gap3")
        self.assertIsNone(self.session.selected_group)
        self.assertEqual(self.session.stable_since, self.start + timedelta(seconds=31))

    def test_trigger_is_percentage_increase_then_two_activity_ticks_and_confirmation(self):
        self.start_stable_over3()
        increased = over3_percentages()
        increased[0] = 8.1
        update = self.add(17, increased, "e4")
        self.assertTrue(update.percentage_triggered)
        self.assertTrue(self.session.percentage_triggered)
        self.assertTrue(self.session.skip_current_tick)

        # The tick that produced the frequency snapshot is not counted.
        self.session.on_tick(2)
        self.assertEqual(self.session.pattern_tracker.progress, 0)
        self.assertFalse(self.session.on_tick(0).pattern_completed)
        self.assertFalse(self.session.on_tick(2).pattern_completed)
        update = self.session.on_tick(7)
        self.assertTrue(update.pattern_completed)
        self.assertTrue(self.session.signal_generated)
        self.assertEqual(self.session.state, SessionState.SELECTED)

    def test_selected_setup_is_cancelled_if_red_green_changes(self):
        self.start_stable_over3()
        changed = dict(over3_percentages())
        changed[5] = 11.0
        update = self.add(17, changed, "e4")
        self.assertEqual(update.state, SessionState.WAITING_FOR_MORE_DATA)
        self.assertIsNone(self.session.selected_group)
        self.assertFalse(self.session.percentage_triggered)
        self.assertEqual(self.session.stable_since, self.start + timedelta(seconds=17))

    def test_stop_prevents_future_observations_and_ticks(self):
        self.session.stop()
        update = self.add(1, over3_percentages(), "stopped")
        self.assertEqual(update.state, SessionState.STOPPED)
        self.assertEqual(self.session.observations, [])


if __name__ == "__main__":
    unittest.main()
