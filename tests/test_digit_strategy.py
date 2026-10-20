import unittest

from app.strategy.digit_strategy import (
    Decision, Group, PatternTracker, evaluate_stability, evaluate_thresholds,
)


def percentages(value=8.0):
    return {digit: value for digit in range(10)}


class ThresholdTests(unittest.TestCase):
    def test_normal_percentages_pass(self):
        p = {0: 8.2, 1: 9.1, 2: 9.5, 3: 9.7, 4: 9.9}
        self.assertTrue(evaluate_thresholds(p, Group.UNDER)[0])

    def test_one_digit_between_10_and_10_5_passes(self):
        p = {0: 8.2, 1: 9.1, 2: 9.5, 3: 9.7, 4: 10.3}
        self.assertTrue(evaluate_thresholds(p, Group.UNDER)[0])

    def test_two_digits_at_least_10_fail(self):
        p = {0: 10.0, 1: 10.1, 2: 9.5, 3: 9.7, 4: 9.9}
        self.assertFalse(evaluate_thresholds(p, Group.UNDER)[0])

    def test_10_5_fails(self):
        p = {0: 10.5, 1: 9.1, 2: 9.5, 3: 9.7, 4: 9.9}
        self.assertFalse(evaluate_thresholds(p, Group.UNDER)[0])


class StabilityTests(unittest.TestCase):
    def test_spread_exactly_point_five_passes(self):
        rows = [{d: 5.5 for d in range(10)}, {d: 6.0 for d in range(10)}]
        self.assertTrue(evaluate_stability(rows, Group.UNDER)[0])

    def test_spread_point_six_fails(self):
        rows = [{d: 5.5 for d in range(10)}, {d: 6.1 for d in range(10)}]
        self.assertFalse(evaluate_stability(rows, Group.UNDER)[0])

    def test_no_observations_fail_closed(self):
        self.assertFalse(evaluate_stability([], Group.UNDER)[0])


class PatternTests(unittest.TestCase):
    def test_under_pattern_matches(self):
        tracker = PatternTracker(Group.UNDER, 2)
        self.assertEqual([tracker.on_tick(d) for d in [2, 1, 4, 7]], [False, False, False, True])

    def test_over_pattern_matches(self):
        tracker = PatternTracker(Group.OVER, 8)
        self.assertEqual([tracker.on_tick(d) for d in [8, 6, 9, 3]], [False, False, False, True])

    def test_failed_attempt_requires_trigger_again(self):
        tracker = PatternTracker(Group.UNDER, 2)
        results = [tracker.on_tick(d) for d in [2, 7, 1, 2, 1, 4, 7]]
        self.assertEqual(results, [False, False, False, False, False, False, True])


if __name__ == "__main__":
    unittest.main()
