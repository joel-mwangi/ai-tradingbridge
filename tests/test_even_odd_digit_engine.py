import pytest

from app.even_odd.digit_engine import analyze_digits


def test_analysis_exposes_even_odd_and_individual_digit_percentage_momentum():
    result = analyze_digits([1, 3, 5, 7, 0, 2, 4, 6])
    sample = result["sample"]

    assert sample["even_percentage_momentum_pp"] == 100.0
    assert sample["odd_percentage_momentum_pp"] == -100.0
    assert sample["digit_percentage_momentum_pp"]["0"] == 25.0
    assert sample["digit_percentage_momentum_pp"]["1"] == -25.0
    assert sample["movement_comparison"] == {
        "previous_half_size": 4,
        "recent_half_size": 4,
    }


def test_analysis_exposes_percentage_acceleration_for_parity_and_digits():
    result = analyze_digits([1, 3, 5, 7, 1, 3, 0, 2])
    sample = result["sample"]

    assert sample["even_percentage_acceleration_pp"] == 100.0
    assert sample["digit_percentage_acceleration_pp"]["0"] == 50.0
    assert sample["digit_percentage_acceleration_pp"]["1"] == 0.0


def test_analysis_exposes_full_digit_transition_matrix_and_conditional_rates():
    result = analyze_digits([1, 2, 1, 3, 1, 2, 4])
    transitions = result["sample"]["digit_transitions"]

    assert transitions["source_counts"]["1"] == 3
    assert transitions["counts"]["1"]["2"] == 2
    assert transitions["counts"]["1"]["3"] == 1
    assert transitions["conditional_probabilities"]["1"]["2"] == pytest.approx(2 / 3)
    assert transitions["conditional_probabilities"]["1"]["3"] == pytest.approx(1 / 3)


def test_analysis_exposes_parity_transition_conditionals_and_latest_suffix_patterns():
    result = analyze_digits([0, 1, 0, 1, 0, 1])
    sample = result["sample"]

    assert sample["parity_transition_conditional_probabilities"]["even"]["odd"] == 1.0
    assert sample["parity_transition_conditional_probabilities"]["odd"]["even"] == 1.0
    assert sample["latest_parity_suffix_patterns"]["2"]["pattern"] == "EO"
    assert sample["latest_parity_suffix_patterns"]["2"]["occurrences"] == 3
    assert sample["latest_parity_suffix_patterns"]["2"]["rate"] == pytest.approx(3 / 5)


def test_analysis_exposes_current_and_longest_digit_streaks():
    result = analyze_digits([1, 1, 2, 2, 2, 4, 4])
    sample = result["sample"]

    assert sample["current_digit_streak"] == 2
    assert sample["longest_digit_streaks"]["2"] == 3


def test_single_observation_does_not_claim_percentage_momentum():
    result = analyze_digits([4])
    sample = result["sample"]

    assert sample["even_percentage_momentum_pp"] == 0.0
    assert sample["odd_percentage_momentum_pp"] == 0.0
    assert all(value == 0.0 for value in sample["digit_percentage_momentum_pp"].values())


def test_analysis_preserves_rolling_windows_and_rejects_invalid_digits():
    result = analyze_digits([index % 10 for index in range(100)])
    assert set(result["rolling_windows"]) == {"50", "100"}
    assert result["rolling_windows"]["50"]["sample_size"] == 50

    with pytest.raises(ValueError, match="At least one"):
        analyze_digits([])
    with pytest.raises(ValueError, match="integer from 0 to 9"):
        analyze_digits([0, True, 2])
