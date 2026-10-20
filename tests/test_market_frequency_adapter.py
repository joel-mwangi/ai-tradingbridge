from datetime import datetime, timezone

import pytest

from app.strategy.market_frequency_adapter import SOURCE_ID, to_percentage_snapshot


def payload():
    return {
        "source": SOURCE_ID,
        "symbol": "1HZ100V",
        "calculated_at": 1_800_000_000.0,
        "latest_tick_epoch": 1_800_000_000,
        "windows": {
            "1000": {
                "window_size": 1000,
                "sample_size": 100,
                "counts": {str(d): 10 for d in range(10)},
                "percentages": {str(d): 10.0 for d in range(10)},
            }
        },
    }


def test_converts_backend_frequency_into_strategy_snapshot():
    received = datetime.fromtimestamp(1_800_000_001, timezone.utc)
    result = to_percentage_snapshot(payload(), received)
    assert result.source_id == SOURCE_ID
    assert result.market_id == "1HZ100V"
    assert result.percentages[0] == 10.0
    assert result.observed_at.timestamp() == 1_800_000_000.0


def test_rejects_non_backend_or_incomplete_frequency_payload():
    data = payload()
    data["source"] = "official_deriv_digit_analytics"
    with pytest.raises(ValueError, match="Unexpected frequency source"):
        to_percentage_snapshot(data)


def test_rejects_insufficient_tick_sample():
    data = payload()
    data["windows"]["1000"]["sample_size"] = 1
    with pytest.raises(ValueError, match="At least two"):
        to_percentage_snapshot(data)
