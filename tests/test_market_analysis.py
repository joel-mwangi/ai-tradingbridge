import pytest

from app.market_analysis import DigitFrequencyWindow, extract_last_digit


@pytest.mark.parametrize(("quote", "pip_size", "expected"), [
    ("123.456", 3, 6),
    ("123.450", 3, 0),
    ("100.09", 2, 9),
    (123.45, 2, 5),
    (123, None, None),
    ("not-a-price", 2, None),
    ("1.2", 13, None),
])
def test_extract_last_digit_preserves_provider_precision(quote, pip_size, expected):
    assert extract_last_digit(quote, pip_size) == expected


def test_frequency_windows_return_counts_and_percentages():
    window = DigitFrequencyWindow()
    window.seed_history(["1.1", "1.2", "1.1", "1.3"], [1, 2, 3, 4], 1)
    result = window.snapshot("R_100")
    summary = result["windows"]["50"]
    assert summary["sample_size"] == 4
    assert summary["counts"]["1"] == 2
    assert summary["percentages"]["1"] == 50.0
    assert sum(summary["counts"].values()) == summary["sample_size"]


def test_window_rolls_over_and_skips_invalid_digits_in_denominator():
    window = DigitFrequencyWindow()
    window.seed_history(["1.1", "1.2", "1.3"], [1, 2, 3], 1)
    window.add_tick("1.4", 4, 1)
    window.add_tick("1.5", 5, 1)
    summary = window.snapshot("R_100")["windows"]["50"]
    assert summary["sample_size"] == 5
    assert summary["counts"]["5"] == 1


def test_latest_duplicate_history_live_tick_is_not_counted_twice():
    window = DigitFrequencyWindow()
    window.seed_history(["1.2"], [123], 1)
    window.add_tick("1.2", 123, 1)
    assert window.snapshot("R_100")["windows"]["50"]["sample_size"] == 1


def test_live_tick_integrity_rejects_duplicate_and_out_of_order_ticks():
    window = DigitFrequencyWindow()
    assert window.add_tick("1.1", 100, 1) == "accepted"
    assert window.add_tick("1.1", 100, 1) == "duplicate"
    assert window.add_tick("1.2", 99, 1) == "out_of_order"
    assert window.add_tick("1.2", 101, 1) == "accepted"
    assert window.snapshot("R_100")["latest_tick_event_id"] == "101.000000:1.2"


def test_distinct_live_ticks_with_same_second_timestamp_are_accepted():
    window = DigitFrequencyWindow()
    assert window.add_tick("1.1", 100, 1, "tick-A") == "accepted"
    assert window.add_tick("1.2", 100, 1, "tick-B") == "accepted"
    summary = window.snapshot("R_100")["windows"]["50"]
    assert summary["sample_size"] == 2
    assert summary["counts"]["1"] == 1
    assert summary["counts"]["2"] == 1
    assert window.snapshot("R_100")["latest_tick_event_id"] == "provider:tick-B"


def test_repeated_provider_event_id_is_rejected_even_with_same_timestamp():
    window = DigitFrequencyWindow()
    assert window.add_tick("1.1", 100, 1, "tick-A") == "accepted"
    assert window.add_tick("1.2", 100, 1, "tick-A") == "duplicate"
    assert window.snapshot("R_100")["windows"]["50"]["sample_size"] == 1


def test_equal_timestamp_does_not_allow_older_timestamp_afterward():
    window = DigitFrequencyWindow()
    assert window.add_tick("1.1", 100, 1, "tick-A") == "accepted"
    assert window.add_tick("1.2", 100, 1, "tick-B") == "accepted"
    assert window.add_tick("1.3", 99, 1, "tick-C") == "out_of_order"



def test_history_arriving_before_live_tick_deduplicates_provider_id_overlap():
    window = DigitFrequencyWindow()
    window.seed_history(["1.2", "1.3"], [123, 124], 1)

    # The provider ID differs from the synthetic history ID, but the tick
    # fingerprint matches the historical row.
    assert window.add_tick("1.2", 123, 1, "provider-123") == "history_overlap"
    assert window.add_tick("1.4", 125, 1, "provider-125") == "accepted"
    summary = window.snapshot("R_100")["windows"]["50"]
    assert summary["sample_size"] == 3
    assert summary["counts"]["2"] == 1
    assert summary["counts"]["3"] == 1
    assert summary["counts"]["4"] == 1


def test_history_arriving_after_live_tick_does_not_double_count_it():
    window = DigitFrequencyWindow()
    assert window.add_tick("1.2", 123, 1, "provider-123") == "accepted"
    window.seed_history(["1.2", "1.3"], [123, 124], 1)

    summary = window.snapshot("R_100")["windows"]["50"]
    assert summary["sample_size"] == 2
    assert summary["counts"]["2"] == 1
    assert summary["counts"]["3"] == 1


def test_identical_live_tick_after_history_overlap_is_consumed_only_once():
    window = DigitFrequencyWindow()
    window.seed_history(["1.2"], [123], 1)
    assert window.add_tick("1.2", 123, 1, "provider-123") == "history_overlap"
    # A second event with same second-resolution timestamp and quote is not
    # suppressed by the already-consumed overlap marker.
    assert window.add_tick("1.2", 123, 1, "provider-124") == "accepted"
    assert window.snapshot("R_100")["windows"]["50"]["sample_size"] == 2
