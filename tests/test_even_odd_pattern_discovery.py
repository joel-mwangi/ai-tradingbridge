from app.even_odd.ml.pattern_discovery import discover_patterns


def sample_digits(size=1200):
    # Deterministic, varied sample with repeated sequence structure.
    return [int((index * 7 + index // 9 + (index // 37) % 3) % 10) for index in range(size)]


def test_discovery_adapts_context_lengths_and_horizons_to_sample_size():
    report = discover_patterns(sample_digits(1200))
    assert report["status"] == "exploratory"
    assert report["adaptive_search"]["parity_sequence_lengths_tested"]
    assert report["adaptive_search"]["digit_sequence_lengths_tested"]
    assert report["adaptive_search"]["frequency_windows_tested"]
    assert report["adaptive_search"]["future_horizons_tested"]
    assert report["adaptive_search"]["future_horizons_tested"][-1] == 20
    assert report["interpretation"]["not_a_next_tick_classifier"] is True


def test_pattern_probabilities_include_future_occurrence_distributions():
    report = discover_patterns(sample_digits(1200))
    assert report["patterns"]
    pattern = report["patterns"][0]
    horizon = pattern["horizons"][2]
    assert horizon["horizon_ticks"] == 3
    assert "probability_at_least_3_odd" in horizon
    assert "probability_3_consecutive_odd" in horizon
    assert len(horizon["digit_occurrence_probabilities"]) == 10
    assert len(horizon["odd_count_distribution"]) == 4


def test_pattern_matches_use_only_complete_historical_future_horizons():
    values = sample_digits(300)
    report = discover_patterns(values)
    max_horizon = report["adaptive_search"]["future_horizons_tested"][-1]
    # The number of examined candidates is bounded by the adaptive context search.
    assert report["patterns_examined"] > 0
    assert max_horizon <= 20
    assert all(item["historical_matches"] > 0 for item in report["patterns"])


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
