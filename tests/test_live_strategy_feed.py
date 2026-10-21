from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app.strategy.live_feed import LiveStrategyFeed
from app.strategy.session import SessionState


def frequency_payload(symbol, timestamp, event_epoch):
    # Counts sum to 1,000 and percentages are derived from those counts.
    # Under uniquely qualifies; digit 4 is its lowest-frequency digit.
    counts = {0: 99, 1: 98, 2: 97, 3: 97, 4: 95, 5: 101, 6: 102, 7: 103, 8: 104, 9: 104}
    percentages = {str(d): round(counts[d] / 1000 * 100, 1) for d in range(10)}
    return {
        "source": "tradebridge_backend_tick_frequency",
        "symbol": symbol,
        "calculated_at": timestamp.timestamp(),
        "latest_tick_epoch": event_epoch,
        "latest_tick_event_id": f"{event_epoch:.6f}:123.45",
        "windows": {
            "1000": {
                "window_size": 1000,
                "sample_size": 1000,
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
    assert status["state"] == "blocked"
    assert status["symbol"] == "1HZ100V"
    assert status["execution_enabled"] is False
    assert status["observation_count"] == 0
    assert "no verified Deriv Digit Analytics percentage feed" in status["reason"]
    assert "disconnected" == status["feed_state"]


def test_unverified_tick_frequency_cannot_qualify_strategy_group():
    start = datetime(2026, 10, 21, 2, 0, tzinfo=timezone.utc)
    clock = [start]
    runtime = LiveStrategyFeed()
    with patch.object(LiveStrategyFeed, "_now", side_effect=lambda: clock[0]):
        status = runtime.start("1HZ100V")

        clock[0] = start + timedelta(seconds=10)
        result = runtime.on_frequency(
            frequency_payload("1HZ100V", clock[0], start.timestamp() + 10)
        )

    assert status["state"] == "blocked"
    assert result is None
    assert runtime.session is not None
    assert runtime.session.state is SessionState.BLOCKED
    assert runtime.session.selected_group is None
    assert runtime.session.observations == []
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
    assert runtime.session.state is SessionState.BLOCKED
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
