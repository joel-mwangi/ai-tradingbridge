from app.even_odd.ml.pattern_discovery import discover_patterns


def sample_digits(size=1200):
    return [int((index * 7 + index // 9 + (index // 37) % 3) % 10) for index in range(size)]


def test_discovery_adapts_context_lengths_and_horizons_to_sample_size():
    report = discover_patterns(sample_digits(1200))
    assert report["status"] == "exploratory"
    assert report["adaptive_search"]["parity_sequence_lengths_tested"]
    assert report["adaptive_search"]["digit_sequence_lengths_tested"]
    assert report["adaptive_search"]["digit_sequence_lengths_tested"][-1] == 20
    assert report["adaptive_search"]["frequency_windows_tested"]
    assert report["adaptive_search"]["future_horizons_tested"][-1] == 34
    assert report["interpretation"]["not_a_next_tick_classifier"] is True


def test_pattern_probabilities_include_future_occurrence_distributions():
    report = discover_patterns(sample_digits(1200))
    assert report["patterns"]
    pattern = report["patterns"][0]
    horizon = pattern["horizons"][2]
    assert horizon["horizon_ticks"] == 3
    assert "probability_at_least_3_odd" in horizon
    assert "probability_3_consecutive_odd" in horizon
    assert len(horizon["odd_count_distribution"]) == 4
    digit_horizon = pattern["horizons"][4]
    assert digit_horizon["horizon_ticks"] == 5
    assert len(digit_horizon["digit_occurrence_probabilities"]) == 10
    assert "holdout_horizon_selection" in pattern


def test_horizon_selection_uses_chronological_holdout_and_reports_status():
    report = discover_patterns(sample_digits(1200))
    selections = [
        pattern["holdout_horizon_selection"]
        for pattern in report["patterns"]
        if pattern["holdout_horizon_selection"]["status"] != "insufficient_occurrences"
    ]
    assert selections
    assert all(item["training_matches"] >= 10 and item["holdout_matches"] >= 10 for item in selections)
    assert all("candidate_horizons_evaluated" in item for item in selections)


def test_insufficient_data_is_explicit():
    report = discover_patterns(sample_digits(99))
    assert report["status"] == "insufficient_data"
    assert report["patterns"] == []


def test_invalid_digits_are_rejected():
    try:
        discover_patterns([0, 1, 2, 10])
    except ValueError as exc:
        assert "integer from 0 to 9" in str(exc)
    else:
        raise AssertionError("Invalid digit sequence should fail validation")
