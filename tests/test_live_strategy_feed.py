from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app.strategy.live_feed import LiveStrategyFeed
from app.strategy.session import SessionState


def frequency_payload(symbol, timestamp, event_epoch, window_size=1000):
    # Counts sum to 1,000 and percentages are derived from those counts.
    # Under uniquely qualifies; digit 4 is its lowest-frequency digit.
    base_counts = {0: 99, 1: 98, 2: 97, 3: 97, 4: 95, 5: 101, 6: 102, 7: 103, 8: 104, 9: 104}
    counts = {d: round(base_counts[d] * window_size / 1000) for d in range(10)}
    counts[0] += window_size - sum(counts.values())
    percentages = {str(d): round(counts[d] / window_size * 100, 1) for d in range(10)}
    return {
        "source": "tradebridge_backend_tick_frequency",
        "symbol": symbol,
        "calculated_at": timestamp.timestamp(),
        "latest_tick_epoch": event_epoch,
        "latest_tick_event_id": f"{event_epoch:.6f}:123.45",
        "windows": {
            str(window_size): {
                "window_size": window_size,
                "sample_size": window_size,
                "counts": {str(d): counts[d] for d in range(10)},
                "percentages": percentages,
            }
        },
        "warning": "TradeBridge-calculated observed-tick frequencies, not official Deriv Digit Analytics.",
    }


def test_start_creates_fresh_observation_only_session():
    now = datetime(2026, 10, 21, 2, 0, tzinfo=timezone.utc)
    with patch.object(LiveStrategyFeed, "_now", return_value=now):
        runtime = LiveStrategyFeed()
        status = runtime.start("1HZ100V")
    assert status["state"] == "observing"
    assert status["symbol"] == "1HZ100V"
    assert status["execution_enabled"] is False
    assert status["digit_window_size"] == 1000
    assert status["observation_count"] == 0
    assert "Waiting for a fresh TradeBridge backend frequency snapshot" in status["reason"]


def test_backend_frequency_snapshot_is_consumed_in_observation_only_mode():
    start = datetime(2026, 10, 21, 2, 0, tzinfo=timezone.utc)
    clock = [start]
    runtime = LiveStrategyFeed()
    with patch.object(LiveStrategyFeed, "_now", side_effect=lambda: clock[0]):
        status = runtime.start("1HZ100V")
        clock[0] = start + timedelta(seconds=9)
        runtime.on_frequency(
            frequency_payload("1HZ100V", clock[0], start.timestamp() + 9)
        )
        clock[0] = start + timedelta(seconds=10)
        result = runtime.on_frequency(
            frequency_payload("1HZ100V", clock[0], start.timestamp() + 10)
        )

    assert status["state"] == "observing"
    assert result is not None
    assert result["symbol"] == "1HZ100V"
    assert result["execution_enabled"] is False
    assert runtime.session is not None
    assert runtime.session.state is not SessionState.BLOCKED
    assert runtime._status("safety check")["execution_enabled"] is False


def test_history_event_before_start_does_not_enter_observation_window():
    start = datetime(2026, 10, 21, 2, 0, tzinfo=timezone.utc)
    runtime = LiveStrategyFeed()
    with patch.object(LiveStrategyFeed, "_now", return_value=start):
        runtime.start("1HZ100V")
        result = runtime.on_frequency(
            frequency_payload("1HZ100V", start - timedelta(seconds=2), start.timestamp() - 2)
        )
    assert result is None
    assert runtime.session is not None
    assert runtime.session.state is SessionState.OBSERVING
    assert runtime.session.observations == []


def test_wrong_market_events_are_ignored_not_mixed_into_session():
    start = datetime(2026, 10, 21, 2, 0, tzinfo=timezone.utc)
    runtime = LiveStrategyFeed()
    with patch.object(LiveStrategyFeed, "_now", return_value=start):
        runtime.start("1HZ100V")
        result = runtime.on_frequency(frequency_payload("R_100", start, start.timestamp()))
    assert result is None
    assert runtime.session is not None
    assert runtime.session.observations == []


def test_stop_prevents_future_strategy_processing():
    start = datetime(2026, 10, 21, 2, 0, tzinfo=timezone.utc)
    runtime = LiveStrategyFeed()
    with patch.object(LiveStrategyFeed, "_now", side_effect=lambda: start):
        runtime.start("1HZ100V")
        stopped = runtime.stop()
        result = runtime.on_frequency(frequency_payload("1HZ100V", start + timedelta(seconds=1), start.timestamp() + 1))
    assert stopped["state"] == "stopped"
    assert result is None
    assert runtime.session is not None
    assert runtime.session.state is SessionState.STOPPED
    assert stopped["execution_enabled"] is False


def test_strategy_uses_the_selected_digit_window_for_snapshots():
    start = datetime(2026, 10, 21, 2, 0, tzinfo=timezone.utc)
    runtime = LiveStrategyFeed()
    with patch.object(LiveStrategyFeed, "_now", return_value=start):
        status = runtime.start("1HZ100V", window_size=100)
        result = runtime.on_frequency(
            frequency_payload("1HZ100V", start + timedelta(seconds=1), start.timestamp() + 1, window_size=100)
        )
    assert status["digit_window_size"] == 100
    assert result is not None
    assert result["execution_enabled"] is False



def test_start_uses_the_selected_barrier_and_fifteen_second_window():
    runtime = LiveStrategyFeed()
    status = runtime.start("1HZ100V", window_size=250, barrier=6)
    assert status["barrier"] == 6
    assert status["digit_window_size"] == 250
    assert status["window_seconds"] == 15.0
    assert runtime.session is not None
    assert runtime.session.barrier == 6


def test_strategy_rejects_non_digit_barrier():
    runtime = LiveStrategyFeed()
    try:
        runtime.start("1HZ100V", barrier=10)
    except ValueError as exc:
        assert "integer digit from 0 to 9" in str(exc)
    else:
        raise AssertionError("Invalid barrier should be rejected")


def test_strategy_rejects_unsupported_selected_digit_window():
    runtime = LiveStrategyFeed()
    try:
        runtime.start("1HZ100V", window_size=75)
    except ValueError as exc:
        assert "one of 50, 100, 250, 500, or 1000" in str(exc)
    else:
        raise AssertionError("Unsupported digit window should be rejected")
