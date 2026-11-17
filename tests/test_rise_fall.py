from app.strategy.rise_fall import RiseFallStrategy, scan_rise_fall


def test_rise_signal_requires_confirmed_monotonic_prices():
    strategy = RiseFallStrategy("rise", fast_period=2, slow_period=4, rsi_period=3, breakout_period=3)
    strategy.seed([100, 100.1, 100.2, 100.3, 100.4, 100.5])
    assert strategy.on_price(100.6) == "CALL"


def test_fall_signal_and_scanner():
    strategy = RiseFallStrategy("fall", fast_period=2, slow_period=4, rsi_period=3, breakout_period=3)
    strategy.seed([10, 9.9, 9.8, 9.7, 9.6, 9.5])
    assert strategy.on_price(9.4) == "PUT"
    assert scan_rise_fall([100 + i * 0.1 for i in range(20)]) == "CALL"
    assert scan_rise_fall([100 - i * 0.1 for i in range(20)]) == "PUT"


def test_rise_signal_requires_strong_closed_two_minute_candle_and_trend():
    strategy = RiseFallStrategy("rise")
    epoch = 1_700_000_000
    for index in range(5):
        start = epoch + index * 120
        strategy.on_tick(100 + index, start)
        strategy.on_tick(100 + index + 0.8, start + 60)
    strategy.on_tick(105.0, epoch + 5 * 120)
    signal = strategy.on_tick(106.5, epoch + 5 * 120 + 60)
    if signal is None:
        signal = strategy.on_tick(106.6, epoch + 6 * 120)
    assert signal == "CALL"
