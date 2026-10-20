from datetime import datetime, timezone

import pytest

from app.strategy.market_frequency_adapter import SOURCE_ID, STRATEGY_WINDOW, to_percentage_snapshot


def payload():
    return {
        "source": SOURCE_ID,
        "symbol": "1HZ100V",
        "calculated_at": 1_800_000_000.0,
        "latest_tick_epoch": 1_799_999_999,
        "windows": {
            "1000": {
                "window_size": 1000,
                "sample_size": 100,
                "counts": {str(d): 10 for d in range(10)},
                "percentages": {str(d): 10.0 for d in range(10)},
            }
        },
        "warning": "Locally calculated observed-tick frequencies; not official Deriv Digit Analytics and not a prediction.",
    }


def test_converts_backend_frequency_into_strategy_snapshot():
    received = datetime.fromtimestamp(1_800_000_001, timezone.utc)
    result = to_percentage_snapshot(payload(), received)
    assert result.source_id == SOURCE_ID
    assert result.market_id == "1HZ100V"
    assert result.percentages[0] == 10.0
    assert result.observed_at.timestamp() == 1_800_000_000.0
    assert result.received_at == received
    assert result.event_id == "1HZ100V:1799999999.000000:1800000000.000000:1000"


def test_uses_the_1000_observed_tick_window_by_default():
    assert STRATEGY_WINDOW == "1000"
    data = payload()
    data["windows"]["1000"]["sample_size"] = 1000
    assert to_percentage_snapshot(data).percentages[9] == 10.0


def test_rejects_non_backend_or_incomplete_frequency_payload():
    data = payload()
    data["source"] = "official_deriv_digit_analytics"
    with pytest.raises(ValueError, match="Unexpected frequency source"):
        to_percentage_snapshot(data)


def test_rejects_insufficient_or_impossible_sample_size():
    data = payload()
    data["windows"]["1000"]["sample_size"] = 1
    with pytest.raises(ValueError, match="between 2 and 1000"):
        to_percentage_snapshot(data)

    data = payload()
    data["windows"]["1000"]["sample_size"] = 1001
    with pytest.raises(ValueError, match="between 2 and 1000"):
        to_percentage_snapshot(data)


@pytest.mark.parametrize("bad_value", [None, True, float("nan"), float("inf"), -0.1, 100.1])
def test_rejects_invalid_digit_percentages(bad_value):
    data = payload()
    data["windows"]["1000"]["percentages"]["4"] = bad_value
    with pytest.raises(ValueError, match="Digit 4 percentage"):
        to_percentage_snapshot(data)


@pytest.mark.parametrize("field", ["calculated_at", "latest_tick_epoch"])
def test_rejects_invalid_source_timestamps(field):
    data = payload()
    data[field] = float("nan")
    with pytest.raises(ValueError, match="timestamp"):
        to_percentage_snapshot(data)


def test_rejects_naive_receipt_timestamp():
    with pytest.raises(ValueError, match="timezone-aware"):
        to_percentage_snapshot(payload(), datetime(2026, 10, 20, 12, 0))
