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


def test_full_over3_flow_qualifies_triggers_and_emits_one_signal_without_trading():
    start = datetime(2026, 10, 23, 2, 0, tzinfo=timezone.utc)
    clock = [start]
    runtime = LiveStrategyFeed()
    base_counts = [78, 80, 81, 82, 83, 115, 116, 117, 123, 125]

    def event(seconds, counts):
        observed = start + timedelta(seconds=seconds)
        clock[0] = observed
        return {
            "source": "tradebridge_backend_tick_frequency",
            "symbol": "1HZ100V",
            "calculated_at": observed.timestamp(),
            "latest_tick_epoch": observed.timestamp(),
            "latest_tick_event_id": f"{observed.timestamp():.6f}:123.45{seconds % 10}",
            "windows": {
                "1000": {
                    "window_size": 1000,
                    "sample_size": 1000,
                    "counts": {str(d): counts[d] for d in range(10)},
                    "percentages": {str(d): counts[d] / 10 for d in range(10)},
                }
            },
            "warning": "TradeBridge-calculated observed-tick frequencies, not official Deriv Digit Analytics.",
        }

    with patch.object(LiveStrategyFeed, "_now", side_effect=lambda: clock[0]):
        started = runtime.start("1HZ100V", window_size=1000, barrier=3)
        assert started["execution_enabled"] is False
        # Frequent, fresh source events provide more than 15 seconds of stable ranks.
        for seconds in (1, 3, 5, 7, 9, 11, 13, 15, 17):
            status = runtime.on_frequency(event(seconds, base_counts))
        assert status is not None
        assert status["state"] == "selected"
        assert status["selected_group"] == "over"
        assert status["trigger_digit"] == 0
        assert status["qualification"]["over_threshold_passes"] is True
        assert status["qualification"]["over_color_positions_pass"] is True
        assert status["signal_generated"] is False

        # Digit 0 rises from 7.8% to 7.9%; keep the total at 1,000 and ranks stable.
        triggered_counts = [79, 80, 81, 82, 82, 115, 116, 117, 123, 125]
        status = runtime.on_frequency(event(18, triggered_counts))
        assert status is not None
        assert status["percentage_triggered"] is True
        # The tick associated with the triggering frequency snapshot is ignored.
        assert runtime.on_tick("1HZ100V", {"quote": "123.450", "pip_size": 3})["signal_generated"] is False
        clock[0] = start + timedelta(seconds=19)
        runtime.on_tick("1HZ100V", {"quote": "123.450", "pip_size": 3})
        clock[0] = start + timedelta(seconds=20)
        runtime.on_tick("1HZ100V", {"quote": "123.452", "pip_size": 3})
        clock[0] = start + timedelta(seconds=21)
        completed = runtime.on_tick("1HZ100V", {"quote": "123.457", "pip_size": 3})
        assert completed is not None
        assert completed["signal_generated"] is True
        assert completed["execution_enabled"] is False
        assert "no trade was submitted" in completed["reason"]
        # Further ticks cannot generate a second signal for this setup.
        clock[0] = start + timedelta(seconds=22)
        duplicate = runtime.on_tick("1HZ100V", {"quote": "123.457", "pip_size": 3})
        assert duplicate is not None
        assert duplicate["signal_generated"] is True
        assert "duplicate signals are suppressed" in duplicate["reason"]

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
