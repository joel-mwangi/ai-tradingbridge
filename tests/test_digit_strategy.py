import math
import unittest
from datetime import datetime, timedelta, timezone

from app.strategy.digit_strategy import (
    Decision,
    Group,
    PatternTracker,
    TimestampedPercentageObservation,
    choose_group,
    choose_group_from_timestamped_observations,
    activity_digits_for,
    confirmation_digits_for,
    strategy_profile_supported,
    evaluate_stability,
    evaluate_thresholds,
    evaluate_color_positions,
    rank_bar_colors,
    validate_initial_observation_window,
)
from app.strategy.digit_strategy import (
    ContractType,
    FastPatternTracker,
    ReverseSignal,
    ThresholdSignal,
    evaluate_reverse_signal,
    evaluate_threshold_signal,
    evaluate_even_odd_setup,
)
from app.deriv.trading_session import TradingProxySession


def over3_percentages():
    return {
        0: 8.0, 1: 8.2, 2: 8.5, 3: 9.1, 4: 8.4,
        5: 10.0, 6: 10.1, 7: 10.2, 8: 10.8, 9: 10.9,
    }


class EvenOddSetupTests(unittest.TestCase):
    def test_even_setup_requires_asymmetric_confluence(self):
        percentages = {0: 12.0, 2: 12.0, 4: 12.0, 6: 12.0, 8: 10.0,
                       1: 8.4, 3: 8.4, 5: 8.4, 7: 8.4, 9: 8.4}
        result = evaluate_even_odd_setup(percentages, ContractType.EVEN)
        self.assertTrue(result.armed)
        self.assertEqual(result.parity_power, 58.0)

    def test_odd_setup_uses_same_fifty_five_percent_floor(self):
        percentages = {1: 14.0, 3: 14.0, 5: 14.0, 7: 14.0, 9: 14.0,
                       0: 6.0, 2: 6.0, 4: 6.0, 6: 6.0, 8: 6.0}
        result = evaluate_even_odd_setup(percentages, ContractType.ODD)
        self.assertTrue(result.armed)

    def test_maziwa_setup_requires_rising_power_when_history_exists(self):
        percentages = {0: 12.0, 2: 11.0, 4: 11.0, 6: 11.0, 8: 11.0,
                       1: 8.8, 3: 8.8, 5: 8.8, 7: 8.8, 9: 8.8}
        self.assertTrue(evaluate_even_odd_setup(percentages, ContractType.EVEN).armed)
        self.assertFalse(
            evaluate_even_odd_setup(percentages, ContractType.EVEN, 56.0).armed
        )

    def test_maziwa_state_enters_on_first_target_tick_after_three_opposing(self):
        percentages = {0: 12.0, 2: 11.0, 4: 11.0, 6: 11.0, 8: 11.0,
                       1: 8.8, 3: 8.8, 5: 8.8, 7: 8.8, 9: 8.8}
        session = TradingProxySession(allowed_client_requests=set())
        self.assertIsNone(session.update_even_odd_strategy(1, percentages, "DIGITEVEN"))
        self.assertIsNone(session.update_even_odd_strategy(3, percentages, "DIGITEVEN"))
        self.assertIsNone(session.update_even_odd_strategy(5, percentages, "DIGITEVEN"))
        self.assertEqual(
            session.update_even_odd_strategy(2, percentages, "DIGITEVEN"),
            "DIGITEVEN",
        )

    def test_maziwa_cooldown_blocks_for_120_ticks(self):
        session = TradingProxySession(allowed_client_requests=set())
        session.start_even_odd_cooldown()
        percentages = {0: 12.0, 2: 11.0, 4: 11.0, 6: 11.0, 8: 11.0,
                       1: 8.8, 3: 8.8, 5: 8.8, 7: 8.8, 9: 8.8}
        for _ in range(120):
            self.assertIsNone(session.update_even_odd_strategy(1, percentages, "DIGITEVEN"))
        self.assertEqual(session.even_odd_cooldown_ticks, 0)


def under6_percentages():
    return {
        0: 10.9, 1: 10.8, 2: 10.7, 3: 10.6, 4: 10.5,
        5: 8.5, 6: 8.0, 7: 8.2, 8: 8.3, 9: 8.4,
    }


class PercentageThresholdTests(unittest.TestCase):
    def test_all_four_profile_digits_strictly_below_10_pass(self):
        p = over3_percentages()
        p.update({0: 9.0, 1: 9.2, 2: 9.4, 3: 9.9})
        self.assertTrue(evaluate_thresholds(p, Group.OVER, barrier=3)[0])

    def test_three_below_10_and_one_at_10_pass(self):
        p = over3_percentages()
        p.update({0: 9.1, 1: 9.2, 2: 9.6, 3: 10.0})
        self.assertTrue(evaluate_thresholds(p, Group.OVER, barrier=3)[0])

    def test_one_remaining_digit_at_10_5_fails_strict_upper_bound(self):
        p = over3_percentages()
        p.update({0: 8.1, 1: 8.2, 2: 9.6, 3: 10.5})
        self.assertFalse(evaluate_thresholds(p, Group.OVER, barrier=3)[0])

    def test_one_remaining_digit_above_10_5_fails(self):
        p = over3_percentages()
        p.update({0: 8.1, 1: 8.2, 2: 9.6, 3: 10.6})
        self.assertFalse(evaluate_thresholds(p, Group.OVER, barrier=3)[0])

    def test_two_digits_at_or_above_10_fail(self):
        p = over3_percentages()
        p.update({0: 10.0, 1: 10.1, 2: 8.0, 3: 8.5})
        self.assertFalse(evaluate_thresholds(p, Group.OVER, barrier=3)[0])

    def test_under_6_checks_only_digits_6_to_9_and_applies_10_5_cap(self):
        p = under6_percentages()
        p.update({0: math.nan, 9: 10.4})  # Digit 0 is outside the filter; the exception remains below 10.5%.
        self.assertTrue(evaluate_thresholds(p, Group.UNDER, barrier=6)[0])
        p[9] = 10.8
        self.assertFalse(evaluate_thresholds(p, Group.UNDER, barrier=6)[0])

    def test_nan_in_filter_digits_fails(self):
        p = over3_percentages()
        p[2] = math.nan
        self.assertFalse(evaluate_thresholds(p, Group.OVER, barrier=3)[0])

    def test_boolean_is_not_a_percentage(self):
        p = over3_percentages()
        p[1] = True
        self.assertFalse(evaluate_thresholds(p, Group.OVER, barrier=3)[0])


class BarColorTests(unittest.TestCase):
    def test_ranks_all_ten_digits_and_preserves_ties(self):
        p = over3_percentages()
        p[0] = p[2]  # Both are now tied at 8.5 but neither is the global minimum.
        ranks = rank_bar_colors(p)
        self.assertEqual(ranks.red, frozenset({1}))
        self.assertEqual(ranks.green, frozenset({9}))
        self.assertEqual(ranks.yellow, frozenset({4}))

    def test_all_tied_minimum_digits_are_red(self):
        p = over3_percentages()
        p[1] = p[0]
        ranks = rank_bar_colors(p)
        self.assertEqual(ranks.red, frozenset({0, 1}))

    def test_red_green_identity_stability_ignores_percentage_spread(self):
        first = over3_percentages()
        second = dict(first)
        second.update({0: 8.1, 1: 8.3, 2: 8.6, 3: 9.2, 4: 8.5})
        self.assertTrue(evaluate_stability([first, second])[0])

    def test_changed_green_identity_resets_stability(self):
        first = over3_percentages()
        second = dict(first)
        second[5] = 11.0
        second[9] = 10.7
        self.assertFalse(evaluate_stability([first, second])[0])

    def test_red_tie_blocks_stability(self):
        p = over3_percentages()
        p[1] = p[0]
        self.assertFalse(evaluate_stability([p, p])[0])


class AutomaticSelectionTests(unittest.TestCase):
    def test_over3_qualifies_and_chooses_trigger_digit_automatically(self):
        p = over3_percentages()
        result = choose_group(p, [p, p], barrier=3)
        self.assertEqual(result.decision, Decision.SELECT_OVER)
        self.assertEqual(result.trigger_digit, 0)
        self.assertTrue(result.over.threshold_passes)
        self.assertTrue(result.over.stability_passes)
        self.assertTrue(result.over.color_positions_pass)

    def test_under6_qualifies_and_chooses_trigger_digit_automatically(self):
        p = under6_percentages()
        result = choose_group(p, [p, p], barrier=6)
        self.assertEqual(result.decision, Decision.SELECT_UNDER)
        self.assertEqual(result.trigger_digit, 6)
        self.assertTrue(result.under.threshold_passes)
        self.assertTrue(result.under.stability_passes)
        self.assertTrue(result.under.color_positions_pass)

    def test_over3_red_has_priority_when_red_and_yellow_are_in_trigger_range(self):
        p = {0: 8.5, 1: 8.0, 2: 7.8, 3: 8.6, 4: 8.7,
             5: 10.0, 6: 10.1, 7: 10.2, 8: 10.8, 9: 10.9}
        result = choose_group(p, [p, p], barrier=3)
        self.assertEqual(result.decision, Decision.SELECT_OVER)
        self.assertEqual(result.trigger_digit, 2)

    def test_over3_yellow_has_priority_when_red_is_at_barrier_plus_one(self):
        p = {0: 8.4, 1: 8.5, 2: 8.0, 3: 8.6, 4: 7.8,
             5: 10.0, 6: 10.1, 7: 10.2, 8: 10.8, 9: 10.9}
        result = choose_group(p, [p, p], barrier=3)
        self.assertEqual(result.decision, Decision.SELECT_OVER)
        self.assertEqual(result.trigger_digit, 2)

    def test_under6_red_has_priority_when_red_and_yellow_are_in_trigger_range(self):
        p = {0: 12.0, 1: 11.0, 2: 16.0, 3: 9.0, 4: 8.8, 5: 8.7,
             6: 8.2, 7: 8.0, 8: 7.8, 9: 8.5}
        result = choose_group(p, [p, p], barrier=6)
        self.assertEqual(result.decision, Decision.SELECT_UNDER)
        self.assertEqual(result.trigger_digit, 8)

    def test_under6_yellow_has_priority_when_red_is_at_barrier_minus_one(self):
        p = {0: 14.9, 1: 13.0, 2: 12.0, 3: 10.0, 4: 9.0, 5: 7.8,
             6: 8.2, 7: 8.0, 8: 8.5, 9: 8.6}
        result = choose_group(p, [p, p], barrier=6)
        self.assertEqual(result.decision, Decision.SELECT_UNDER)
        self.assertEqual(result.trigger_digit, 7)

    def test_yellow_at_8_point_9_in_over_zone_passes(self):
        p = {0: 8.0, 1: 8.9, 2: 9.5, 3: 9.6, 4: 9.7, 5: 10.0, 6: 10.1, 7: 10.2, 8: 10.8, 9: 10.9}
        ok, _ = evaluate_color_positions(p, Group.OVER, barrier=3)
        self.assertTrue(ok)

    def test_yellow_at_exactly_9_percent_in_over_zone_fails(self):
        p = {0: 8.0, 1: 9.0, 2: 9.5, 3: 9.6, 4: 9.7, 5: 10.0, 6: 10.1, 7: 10.2, 8: 10.8, 9: 10.9}
        ok, _ = evaluate_color_positions(p, Group.OVER, barrier=3)
        self.assertFalse(ok)

    def test_over3_yellow_at_9_percent_fails_when_yellow_is_in_zone(self):
        p = over3_percentages()
        p.update({0: 8.0, 1: 9.0, 2: 9.5, 3: 9.6, 4: 9.7, 5: 10.0, 6: 10.1, 7: 10.2, 8: 10.8, 9: 10.9})
        ok, reason = evaluate_color_positions(p, Group.OVER, barrier=3)
        self.assertFalse(ok)
        self.assertIn("strictly below 9%", reason)

    def test_over5_yellow_at_9_percent_fails_even_when_outside_zone(self):
        # Yellow is outside Over 5's low-side zone, but its <9% threshold still applies.
        p = {0: 8.0, 1: 9.2, 2: 9.3, 3: 9.4, 4: 9.5, 5: 9.6,
             6: 9.7, 7: 10.2, 8: 9.0, 9: 10.5}
        ok, reason = evaluate_color_positions(p, Group.OVER, barrier=5)
        self.assertFalse(ok)
        self.assertIn("strictly below 9%", reason)

    def test_over5_red_alone_is_not_invalidated_by_yellow_outside_zone(self):
        # digit-0 = Red (7.8%, sole minimum, in zone 0..6, < 9%)
        # digit-8 = Yellow (8.0%, second lowest overall, outside zone 0..6)
        # digit-9 = Green (10.5%, highest); digit-7 = Blue (10.2%, second highest)
        p = {
            0: 7.8, 1: 8.5, 2: 8.6, 3: 8.7, 4: 8.8, 5: 9.0,
            6: 9.1, 7: 10.2, 8: 8.0, 9: 10.5,
        }
        ok, reason = evaluate_color_positions(p, Group.OVER, barrier=5)
        self.assertTrue(ok)
        self.assertIn("Red-alone", reason)

    def test_under6_yellow_at_9_percent_fails_even_when_outside_zone(self):
        # Yellow is outside Under 6's high-side zone, but its <9% threshold still applies.
        p = {0: 10.9, 1: 10.8, 2: 10.7, 3: 10.6, 4: 9.0,
             5: 8.0, 6: 9.1, 7: 9.2, 8: 9.3, 9: 9.4}
        ok, reason = evaluate_color_positions(p, Group.UNDER, barrier=6)
        self.assertFalse(ok)
        self.assertIn("strictly below 9%", reason)

    def test_under6_red_alone_qualifies_when_yellow_is_outside_zone(self):
        # Under 6: Red/Yellow qualifying zone is digits 5..9 (B-1..9).
        # Fixture: digit-5 = Red (8.0%, lowest, in zone), digit-4 = Yellow
        # (8.1%, second-lowest, outside zone) → Red-alone qualification path.
        p = {0: 10.9, 1: 10.8, 2: 10.7, 3: 10.6, 4: 8.1,
             5: 8.0, 6: 8.2, 7: 8.8, 8: 9.3, 9: 9.4}
        ok, reason = evaluate_color_positions(p, Group.UNDER, barrier=6)
        self.assertTrue(ok)
        self.assertIn("Red-alone qualification", reason)

    def test_under_red_alone_qualifies_when_yellow_outside_zone(self):
        # Under 6: high-side zone is 5..9; low-side is 0..4.
        # Red=digit-5 (8.0%, unique min, in-zone 5..9, <9%), Yellow=digit-4 (8.1%, outside zone 5..9).
        p = {0: 10.9, 1: 10.8, 2: 10.7, 3: 10.6, 4: 8.1,
             5: 8.0, 6: 8.2, 7: 8.8, 8: 9.3, 9: 9.4}
        ok, _ = evaluate_color_positions(p, Group.UNDER, barrier=6)
        self.assertTrue(ok)

    def test_under6_red_exactly_9_percent_fails(self):
        p = {0: 10.9, 1: 10.8, 2: 10.7, 3: 10.6, 4: 10.5,
             5: 9.2, 6: 9.0, 7: 9.1, 8: 9.3, 9: 9.4}
        ok, reason = evaluate_color_positions(p, Group.UNDER, barrier=6)
        self.assertFalse(ok)
        self.assertIn("strictly below 9%", reason)

    def test_red_tie_prevents_arbitrary_selection(self):
        p = over3_percentages()
        p[1] = p[0]
        result = choose_group(p, [p, p], barrier=3)
        self.assertNotEqual(result.decision, Decision.SELECT_OVER)

    def test_changed_red_or_green_identity_prevents_selection(self):
        first = over3_percentages()
        second = dict(first)
        second[5] = 11.0
        second[9] = 10.7
        result = choose_group(first, [first, second], barrier=3)
        self.assertNotEqual(result.decision, Decision.SELECT_OVER)

    def test_over_red_alone_qualifies_when_yellow_outside_zone(self):
        # Over 5: low-side zone is 0..6; high-side is 7..9.
        # Red=digit-0 (7.8%, unique min, in-zone, <9%), Yellow=digit-8 (8.0%, outside zone 0..6).
        p = {0: 7.8, 1: 8.5, 2: 8.6, 3: 8.7, 4: 8.8, 5: 9.0,
             6: 9.1, 7: 10.2, 8: 8.0, 9: 10.5}
        ok, _ = evaluate_color_positions(p, Group.OVER, barrier=5)
        self.assertTrue(ok)

    def test_invalid_percentage_anywhere_rejects_full_rankings(self):
        p = over3_percentages()
        p[7] = math.inf
        result = choose_group(p, [p, p], barrier=3)
        self.assertEqual(result.decision, Decision.INVALID_DATA)


class TimestampedStabilityTests(unittest.TestCase):
    def setUp(self):
        self.started = datetime(2026, 10, 23, 10, 0, tzinfo=timezone.utc)
        self.values = over3_percentages()
        self.observations = [
            TimestampedPercentageObservation(self.started, self.values),
            TimestampedPercentageObservation(self.started + timedelta(seconds=15), self.values),
        ]

    def test_full_fifteen_second_window_passes(self):
        ok, _ = validate_initial_observation_window(
            self.observations, self.started, self.started + timedelta(seconds=15)
        )
        self.assertTrue(ok)

    def test_fourteen_seconds_is_not_enough(self):
        observations = [
            self.observations[0],
            TimestampedPercentageObservation(self.started + timedelta(seconds=14), self.values),
        ]
        result = choose_group_from_timestamped_observations(
            observations, self.started, self.started + timedelta(seconds=14), barrier=3
        )
        self.assertEqual(result.decision, Decision.INVALID_DATA)

    def test_timestamped_selector_uses_fifteen_seconds_and_selects_over3(self):
        result = choose_group_from_timestamped_observations(
            self.observations, self.started, self.started + timedelta(seconds=15), barrier=3
        )
        self.assertEqual(result.decision, Decision.SELECT_OVER)
        self.assertEqual(result.trigger_digit, 0)

    def test_naive_timestamps_fail(self):
        ok, _ = validate_initial_observation_window(
            self.observations,
            datetime(2026, 10, 23, 10, 0),
            self.started + timedelta(seconds=15),
        )
        self.assertFalse(ok)


class PatternSequenceTests(unittest.TestCase):
    def test_over3_percentage_trigger_then_two_activity_ticks_and_confirmation(self):
        tracker = PatternTracker(Group.OVER, trigger_digit=2, barrier=3)
        self.assertEqual([tracker.on_tick(d) for d in [0, 3, 7]], [False, False, False])
        tracker.arm()
        self.assertEqual([tracker.on_tick(d) for d in [0, 3, 2, 6]], [False, False, False, True])

    def test_under6_sequence_uses_upper_activity_then_lower_confirmation(self):
        tracker = PatternTracker(Group.UNDER, trigger_digit=7, barrier=6)
        tracker.arm()
        self.assertEqual([tracker.on_tick(d) for d in [6, 8, 2]], [False, False, True])

    def test_one_activity_tick_followed_by_confirmation_completes(self):
        tracker = PatternTracker(Group.UNDER, trigger_digit=6, barrier=6)
        tracker.arm()
        self.assertFalse(tracker.on_tick(6))
        self.assertTrue(tracker.on_tick(2))
        self.assertEqual(tracker.completed_patterns, 1)

    def test_boolean_tick_and_trigger_are_rejected(self):
        tracker = PatternTracker(Group.OVER, trigger_digit=0)
        with self.assertRaises(ValueError):
            tracker.on_tick(True)
        with self.assertRaises(ValueError):
            PatternTracker(Group.OVER, trigger_digit=True)



class BarrierGeneralizationTests(unittest.TestCase):
    def test_over_filter_changes_with_selected_barrier(self):
        from app.strategy.digit_strategy import digits_for
        self.assertEqual(digits_for(Group.OVER, 3), frozenset({0, 1, 2, 3}))
        self.assertEqual(digits_for(Group.OVER, 6), frozenset({0, 1, 2, 3, 4, 5, 6}))

    def test_under_filter_changes_with_selected_barrier(self):
        from app.strategy.digit_strategy import digits_for
        self.assertEqual(digits_for(Group.UNDER, 6), frozenset({6, 7, 8, 9}))
        self.assertEqual(digits_for(Group.UNDER, 3), frozenset({3, 4, 5, 6, 7, 8, 9}))

    def test_generalized_over6_percentage_filter_uses_selected_barrier(self):
        p = {d: 8.0 for d in range(10)}
        p[6] = 10.1
        ok, _ = evaluate_thresholds(p, Group.OVER, barrier=6)
        self.assertTrue(ok)

    def test_over4_can_qualify_from_generalized_rules(self):
        p = over3_percentages()
        result = choose_group(p, [p, p], barrier=4)
        self.assertEqual(result.decision, Decision.SELECT_OVER)
        self.assertTrue(result.over.qualifies)

    def test_barrier_profiles_are_generalized_where_sequence_is_possible(self):
        from app.strategy.digit_strategy import strategy_profile_supported
        self.assertTrue(strategy_profile_supported(Group.OVER, 3))
        self.assertTrue(strategy_profile_supported(Group.UNDER, 6))
        self.assertTrue(strategy_profile_supported(Group.OVER, 4))
        self.assertTrue(strategy_profile_supported(Group.UNDER, 3))

    def test_barrier_validation_excludes_impossible_direction(self):
        from app.strategy.digit_strategy import barrier_valid_for
        self.assertTrue(barrier_valid_for(Group.OVER, 0))
        self.assertFalse(barrier_valid_for(Group.UNDER, 0))
        self.assertFalse(barrier_valid_for(Group.OVER, 9))
        self.assertTrue(barrier_valid_for(Group.UNDER, 9))

    def test_pattern_confirmation_adapts_to_selected_barrier(self):
        tracker = PatternTracker(Group.OVER, trigger_digit=6, barrier=6)
        tracker.arm()
        self.assertEqual([tracker.on_tick(d) for d in [0, 4, 9]], [False, False, True])

    def test_generalized_over4_color_positions_pass(self):
        self.assertTrue(evaluate_color_positions(over3_percentages(), Group.OVER, barrier=3)[0])
        self.assertTrue(evaluate_color_positions(over3_percentages(), Group.OVER, barrier=4)[0])

    def test_generalized_under5_color_positions_pass(self):
        self.assertTrue(evaluate_color_positions(under6_percentages(), Group.UNDER, barrier=6)[0])
        self.assertTrue(evaluate_color_positions(under6_percentages(), Group.UNDER, barrier=5)[0])

    def test_barrier_relative_activity_and_confirmation_sets(self):
        self.assertEqual(activity_digits_for(Group.OVER, 4), frozenset({0, 1, 2, 3, 4}))
        self.assertEqual(confirmation_digits_for(Group.OVER, 4), frozenset({7, 8, 9}))
        self.assertEqual(activity_digits_for(Group.UNDER, 5), frozenset({5, 6, 7, 8, 9}))
        self.assertEqual(confirmation_digits_for(Group.UNDER, 5), frozenset({0, 1, 2}))

    def test_profiles_supported_only_when_confirmation_band_is_nonempty(self):
        self.assertTrue(strategy_profile_supported(Group.OVER, 4))
        self.assertTrue(strategy_profile_supported(Group.UNDER, 5))
        self.assertFalse(strategy_profile_supported(Group.OVER, 7))
        self.assertFalse(strategy_profile_supported(Group.UNDER, 2))


class ThresholdSignalTests(unittest.TestCase):
    def _base_percentages(self):
        """10 digits all at 10.0% (uniform)."""
        return {d: 10.0 for d in range(10)}

    def test_threshold_triggered_for_over_when_lower_zone_digit_reaches_12pct(self):
        p = self._base_percentages()
        # digit 1 is in 0..4 (Over 4 zone); elevate it above threshold
        p[1] = 12.0
        # keep all others clearly below 12%
        for d in range(10):
            if d != 1:
                p[d] = 10.4
        result = evaluate_threshold_signal(p, ContractType.OVER, barrier=4)
        self.assertTrue(result.triggered)

    def test_threshold_high_confidence_when_qualifying_digit_is_green(self):
        p = self._base_percentages()
        # digit 1 is both ≥12% AND the unique maximum (green)
        for d in range(10):
            p[d] = 9.0
        p[1] = 14.0   # unique max → green
        result = evaluate_threshold_signal(p, ContractType.OVER, barrier=4)
        self.assertTrue(result.triggered)
        self.assertEqual(result.confidence, "HIGH")

    def test_threshold_medium_confidence_when_qualifying_digit_not_green_or_blue(self):
        p = self._base_percentages()
        # digit 1 ≥12% but NOT the max or second-max
        p[0] = 9.0
        p[1] = 12.5   # qualifies (≥12%) but not max/second-max
        p[2] = 9.0
        p[3] = 9.0
        p[4] = 9.0
        p[5] = 15.0   # green (max)
        p[6] = 14.0   # blue (second-max)
        p[7] = 9.5
        p[8] = 9.6
        p[9] = 9.7
        result = evaluate_threshold_signal(p, ContractType.OVER, barrier=4)
        self.assertTrue(result.triggered)
        self.assertEqual(result.confidence, "MEDIUM")

    def test_threshold_not_triggered_when_no_digit_reaches_12pct(self):
        p = {d: 10.0 for d in range(10)}
        result = evaluate_threshold_signal(p, ContractType.OVER, barrier=4)
        self.assertFalse(result.triggered)
        self.assertEqual(result.confidence, "NONE")

    def test_threshold_triggered_for_under_when_upper_zone_digit_reaches_12pct(self):
        # ContractType.UNDER barrier=6 → scan digits 6..9
        p = {d: 9.0 for d in range(10)}
        p[7] = 12.5   # in 6..9 zone
        result = evaluate_threshold_signal(p, ContractType.UNDER, barrier=6)
        self.assertTrue(result.triggered)

    def test_threshold_triggered_for_even_contract(self):
        p = {d: 9.0 for d in range(10)}
        p[4] = 12.1   # even digit ≥ 12%
        result = evaluate_threshold_signal(p, ContractType.EVEN)
        self.assertTrue(result.triggered)

    def test_threshold_triggered_for_odd_contract(self):
        p = {d: 9.0 for d in range(10)}
        p[5] = 12.3   # odd digit ≥ 12%
        result = evaluate_threshold_signal(p, ContractType.ODD)
        self.assertTrue(result.triggered)


class ReverseSignalTests(unittest.TestCase):
    def _percentages_with_max_at(self, digit: int, pct: float) -> dict[int, float]:
        """Build a percentages map where the given digit is the unique maximum."""
        base = 9.0
        p = {d: base for d in range(10)}
        p[digit] = pct
        # make second-distinct level clear
        for d in range(10):
            if d != digit:
                p[d] = base - (d * 0.01)   # all different, all < pct
        return p

    def test_reverse_suggested_when_green_in_confirming_zone_over_12pct(self):
        # barrier=4, Group.OVER: confirming zone is 5..9; digit 9 at 13.0%
        p = self._percentages_with_max_at(9, 13.0)
        result = evaluate_reverse_signal(p, Group.OVER, barrier=4)
        self.assertTrue(result.suggested)

    def test_reverse_not_suggested_when_green_below_12pct(self):
        p = self._percentages_with_max_at(9, 11.0)
        result = evaluate_reverse_signal(p, Group.OVER, barrier=4)
        self.assertFalse(result.suggested)

    def test_reverse_not_suggested_when_green_in_low_zone(self):
        # digit 2 at 13.0% — in 0..4 (activity zone for Over 4), NOT confirming zone 5..9
        p = self._percentages_with_max_at(2, 13.0)
        result = evaluate_reverse_signal(p, Group.OVER, barrier=4)
        self.assertFalse(result.suggested)


class FastPatternTrackerTests(unittest.TestCase):
    def _build_percentages(self):
        """
        Build percentages with a clear RED digit (0) and clear GREEN digit (9).
        """
        p = {d: 10.0 for d in range(10)}
        p[0] = 7.0    # unique minimum → Red
        p[1] = 8.0    # second-lowest → Yellow
        p[8] = 12.0   # second-highest → Blue
        p[9] = 14.0   # unique maximum → Green
        return p

    def test_fast_tracker_completes_on_low_then_high(self):
        p = self._build_percentages()
        tracker = FastPatternTracker(Group.OVER, barrier=4)
        tracker.arm(p)
        # Red digit (0) is LOW; Green digit (9) is HIGH
        self.assertFalse(tracker.on_tick(0))   # stage 0 → 1
        self.assertTrue(tracker.on_tick(9))    # stage 1 + HIGH → complete

    def test_fast_tracker_does_not_complete_without_low_digit_first(self):
        p = self._build_percentages()
        tracker = FastPatternTracker(Group.OVER, barrier=4)
        tracker.arm(p)
        # Start directly with high digit; should not complete
        self.assertFalse(tracker.on_tick(9))
        self.assertEqual(tracker.completed_patterns, 0)

    def test_fast_tracker_requires_low_before_high(self):
        p = self._build_percentages()
        tracker = FastPatternTracker(Group.OVER, barrier=4)
        tracker.arm(p)
        self.assertFalse(tracker.on_tick(9))   # HIGH first → stays in stage 0
        self.assertFalse(tracker.on_tick(0))   # LOW → stage 1
        self.assertTrue(tracker.on_tick(9))    # HIGH → complete

    def test_fast_tracker_reset_prevents_completion(self):
        p = self._build_percentages()
        tracker = FastPatternTracker(Group.OVER, barrier=4)
        tracker.arm(p)
        tracker.on_tick(0)   # stage 0 → 1
        tracker.reset()
        self.assertFalse(tracker.on_tick(9))   # not armed → False

    def test_fast_tracker_not_armed_before_arm_call(self):
        tracker = FastPatternTracker(Group.OVER, barrier=4)
        # Should return False and not raise
        self.assertFalse(tracker.on_tick(5))
        self.assertFalse(tracker.armed)

    def test_fast_tracker_neutral_digit_in_stage1_resets_to_stage0(self):
        """A neutral digit (neither LOW nor HIGH) in stage 1 must reset to stage 0
        and prevent a HIGH digit fired after it from completing the sequence."""
        p = self._build_percentages()
        # digits 0=Red(LOW), 1=Yellow(LOW), 8=Blue(HIGH), 9=Green(HIGH); 2-7 neutral
        tracker = FastPatternTracker(Group.OVER, barrier=4)
        tracker.arm(p)
        self.assertFalse(tracker.on_tick(0))   # stage 0 → 1 (LOW received)
        self.assertFalse(tracker.on_tick(5))   # neutral → resets to stage 0
        self.assertFalse(tracker.on_tick(9))   # stage 0 + HIGH → no completion


class ReverseSignalUnderTests(unittest.TestCase):
    """Cover Group.UNDER confirming zone in evaluate_reverse_signal."""

    def _percentages_with_max_at(self, digit: int, pct: float) -> dict[int, float]:
        base = 9.0
        p = {d: base for d in range(10)}
        p[digit] = pct
        for d in range(10):
            if d != digit:
                p[d] = base - (d * 0.01)
        return p

    def test_reverse_suggested_when_green_in_confirming_zone_under_barrier5(self):
        # barrier=5, Group.UNDER: confirming zone = range(0, 5) = {0,1,2,3,4}
        # digit 0 at 13.0% → Green, in confirming zone → reverse suggested
        p = self._percentages_with_max_at(0, 13.0)
        result = evaluate_reverse_signal(p, Group.UNDER, barrier=5)
        self.assertTrue(result.suggested)

    def test_reverse_not_suggested_under_when_green_above_barrier(self):
        # digit 7 at 13.0% — in activity zone 5..9, NOT confirming zone 0..4
        p = self._percentages_with_max_at(7, 13.0)
        result = evaluate_reverse_signal(p, Group.UNDER, barrier=5)
        self.assertFalse(result.suggested)

    def test_reverse_not_suggested_under_when_pct_below_12(self):
        p = self._percentages_with_max_at(0, 11.5)
        result = evaluate_reverse_signal(p, Group.UNDER, barrier=5)
        self.assertFalse(result.suggested)


class GroupEvaluationThresholdSignalTests(unittest.TestCase):
    def test_group_evaluation_includes_threshold_signal(self):
        from app.strategy.digit_strategy import evaluate_group, ThresholdSignal
        p = over3_percentages()
        result = evaluate_group(p, [p, p], Group.OVER, barrier=3)
        self.assertTrue(hasattr(result, "threshold_signal"))
        self.assertIsInstance(result.threshold_signal, ThresholdSignal)


if __name__ == "__main__":
    unittest.main()
