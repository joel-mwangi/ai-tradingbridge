from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app.strategy.live_feed import LiveStrategyFeed
from app.strategy.session import SessionState


def frequency_payload(symbol, timestamp, event_epoch):
    under = {0: 9.9, 1: 9.8, 2: 9.7, 3: 9.6, 4: 9.5}
    over = {5: 10.1, 6: 10.2, 7: 10.3, 8: 10.4, 9: 10.4}
    percentages = {str(k): v for k, v in {**under, **over}.items()}
    return {
        "source": "tradebridge_backend_tick_frequency",
        "symbol": symbol,
        "calculated_at": timestamp.timestamp(),
        "latest_tick_epoch": event_epoch,
        "windows": {
            "1000": {
                "window_size": 1000,
                "sample_size": 1000,
                "counts": {str(d): 100 for d in range(10)},
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
    assert status["observation_count"] == 0


def test_live_percentage_events_select_group_after_real_ten_second_window():
    start = datetime(2026, 10, 21, 2, 0, tzinfo=timezone.utc)
    clock = [start]
    runtime = LiveStrategyFeed()
    with patch.object(LiveStrategyFeed, "_now", side_effect=lambda: clock[0]):
        runtime.start("1HZ100V")

        clock[0] = start + timedelta(seconds=1)
        first = runtime.on_frequency(frequency_payload("1HZ100V", clock[0], 1))
        assert first["state"] == "observing"

        clock[0] = start + timedelta(seconds=9, milliseconds=900)
        second = runtime.on_frequency(frequency_payload("1HZ100V", clock[0], 2))
        assert second["state"] == "observing"

        clock[0] = start + timedelta(seconds=10)
        third = runtime.on_frequency(frequency_payload("1HZ100V", clock[0], 3))

    assert third["state"] == "selected"
    assert third["selected_group"] == "under"
    assert third["trigger_digit"] == 4
    assert third["execution_enabled"] is False


def test_history_event_before_start_does_not_enter_observation_window():
    start = datetime(2026, 10, 21, 2, 0, tzinfo=timezone.utc)
    runtime = LiveStrategyFeed()
    with patch.object(LiveStrategyFeed, "_now", return_value=start):
        runtime.start("1HZ100V")
        result = runtime.on_frequency(
            frequency_payload("1HZ100V", start - timedelta(seconds=2), 10)
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
        result = runtime.on_frequency(frequency_payload("R_100", start, 11))
    assert result is None
    assert runtime.session is not None
    assert runtime.session.observations == []


def test_stop_prevents_future_strategy_processing():
    start = datetime(2026, 10, 21, 2, 0, tzinfo=timezone.utc)
    runtime = LiveStrategyFeed()
    with patch.object(LiveStrategyFeed, "_now", return_value=start):
        runtime.start("1HZ100V")
        stopped = runtime.stop()
        result = runtime.on_frequency(frequency_payload("1HZ100V", start + timedelta(seconds=1), 12))
    assert stopped["state"] == "stopped"
    assert result is not None
    assert result["state"] == "stopped"
    assert result["execution_enabled"] is False
