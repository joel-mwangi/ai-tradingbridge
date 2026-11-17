"""Fail-closed trend and breakout strategy for Rise/Fall contracts."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Sequence


@dataclass(frozen=True)
class Candle:
    start: int
    open: float
    high: float
    low: float
    close: float

    @property
    def body_ratio(self) -> float:
        candle_range = self.high - self.low
        return abs(self.close - self.open) / candle_range if candle_range else 0.0


@dataclass(frozen=True)
class RiseFallAnalysis:
    signal: str | None
    fast_ema: float | None
    slow_ema: float | None
    rsi: float | None
    support: float | None
    resistance: float | None
    reason: str


def _ema(prices: Sequence[float], period: int) -> float:
    value = float(prices[0])
    multiplier = 2.0 / (period + 1)
    for price in prices[1:]:
        value = (float(price) - value) * multiplier + value
    return value


def _rsi(prices: Sequence[float], period: int) -> float:
    changes = [float(b) - float(a) for a, b in zip(prices, prices[1:])]
    gains = [max(change, 0.0) for change in changes[-period:]]
    losses = [max(-change, 0.0) for change in changes[-period:]]
    average_gain = sum(gains) / period
    average_loss = sum(losses) / period
    if average_loss == 0:
        return 100.0 if average_gain else 50.0
    return 100.0 - (100.0 / (1.0 + average_gain / average_loss))


@dataclass
class RiseFallStrategy:
    """Trade only when trend, momentum, and breakout agree."""

    direction: str
    fast_period: int = 5
    slow_period: int = 13
    rsi_period: int = 14
    breakout_period: int = 10
    minimum_ticks: int = 3
    candle_seconds: int = 120
    body_ratio: float = 0.60
    trend_candles: int = 5
    state: str = "idle"
    prices: list[float] = field(default_factory=list)
    signal_generated: bool = False
    last_analysis: RiseFallAnalysis | None = None
    candles: list[Candle] = field(default_factory=list)
    _active_candle: Candle | None = None

    def __post_init__(self) -> None:
        if self.direction not in ("rise", "fall"):
            raise ValueError("Rise/Fall direction must be 'rise' or 'fall'.")
        periods = (self.fast_period, self.slow_period, self.rsi_period, self.breakout_period)
        if any(type(period) is not int or period < 2 for period in periods):
            raise ValueError("Rise/Fall indicator periods must be integers of at least 2.")
        if self.fast_period >= self.slow_period:
            raise ValueError("Rise/Fall fast_period must be less than slow_period.")
        if type(self.minimum_ticks) is not int or self.minimum_ticks < 2:
            raise ValueError("Rise/Fall minimum_ticks must be at least 2.")
        if type(self.candle_seconds) is not int or self.candle_seconds < 60:
            raise ValueError("candle_seconds must be at least 60.")
        if not isinstance(self.body_ratio, (int, float)) or not 0 < self.body_ratio <= 1:
            raise ValueError("body_ratio must be greater than 0 and at most 1.")
        if type(self.trend_candles) is not int or self.trend_candles < 2:
            raise ValueError("trend_candles must be at least 2.")
        self.state = "observing"

    @property
    def warmup_ticks(self) -> int:
        return max(self.slow_period + 1, self.rsi_period + 1, self.breakout_period + 1)

    def seed(self, prices: Sequence[object]) -> None:
        """Load provider history without generating a signal."""
        for price in prices:
            try:
                value = float(price)
            except (TypeError, ValueError):
                continue
            if math.isfinite(value):
                self.prices.append(value)
        self.prices = self.prices[-max(self.warmup_ticks, self.breakout_period + 1) * 3 :]

    def seed_ticks(self, prices: Sequence[object], epochs: Sequence[object]) -> None:
        """Seed indicator history and completed candles from provider history."""
        self.seed(prices)
        for price, epoch in zip(prices, epochs):
            self._update_candle(price, epoch, emit_signal=False)
        self._active_candle = None

    def on_tick(self, price: object, epoch: object) -> str | None:
        """Build candles from ticks and signal only after a candle closes."""
        try:
            value = float(price)
            timestamp = int(float(epoch))
        except (TypeError, ValueError, OverflowError):
            return None
        if not math.isfinite(value):
            return None
        self.prices.append(value)
        self.prices = self.prices[-max(self.warmup_ticks, self.breakout_period + 1) * 3 :]
        return self._update_candle(value, timestamp, emit_signal=True)

    def _update_candle(self, value: float, timestamp: int, emit_signal: bool) -> str | None:
        start = timestamp - (timestamp % self.candle_seconds)
        active = self._active_candle
        if active is None or active.start != start:
            if active is not None:
                self.candles.append(active)
                self.candles = self.candles[-(self.trend_candles + 2) :]
                if emit_signal:
                    signal = self._candle_signal()
                    if signal:
                        self.signal_generated = True
                        self.state = "selected"
                        return signal
            self._active_candle = Candle(start, value, value, value, value)
            return None
        self._active_candle = Candle(
            active.start,
            active.open,
            max(active.high, value),
            min(active.low, value),
            value,
        )
        return None

    def _candle_signal(self) -> str | None:
        if len(self.candles) < self.trend_candles + 1 or self.signal_generated:
            return None
        signal_candle = self.candles[-1]
        trend = self.candles[-self.trend_candles - 1 : -1]
        rising = all(a.close < b.close for a, b in zip(trend, trend[1:]))
        falling = all(a.close > b.close for a, b in zip(trend, trend[1:]))
        bullish = signal_candle.close > signal_candle.open and signal_candle.body_ratio >= self.body_ratio
        bearish = signal_candle.close < signal_candle.open and signal_candle.body_ratio >= self.body_ratio
        if self.direction == "rise" and rising and bullish:
            return "CALL"
        if self.direction == "fall" and falling and bearish:
            return "PUT"
        return None

    def on_price(self, price: object) -> str | None:
        try:
            value = float(price)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(value):
            return None
        if self.prices and value == self.prices[-1]:
            return None
        self.prices.append(value)
        self.prices = self.prices[-max(self.warmup_ticks, self.breakout_period + 1) * 3 :]
        if self.signal_generated:
            return None
        analysis = self.analyze()
        self.last_analysis = analysis
        if analysis.signal is not None:
            self.signal_generated = True
            self.state = "selected"
        return analysis.signal

    def analyze(self) -> RiseFallAnalysis:
        if len(self.prices) < self.warmup_ticks:
            return RiseFallAnalysis(None, None, None, None, None, None, "Warming up price history.")
        prices = self.prices
        fast = _ema(prices[-self.slow_period * 2 :], self.fast_period)
        slow = _ema(prices[-self.slow_period * 2 :], self.slow_period)
        rsi = _rsi(prices, self.rsi_period)
        prior = prices[-self.breakout_period - 1 : -1]
        support, resistance = min(prior), max(prior)
        rising = fast > slow and rsi >= 50.0 and prices[-1] > resistance
        falling = fast < slow and rsi <= 50.0 and prices[-1] < support
        signal = (
            "CALL" if self.direction == "rise" and rising
            else "PUT" if self.direction == "fall" and falling
            else None
        )
        reason = "Trend, RSI, and breakout agree." if signal else "Waiting for trend, RSI, and breakout confirmation."
        return RiseFallAnalysis(signal, fast, slow, rsi, support, resistance, reason)

    def status(self, reason: str) -> dict[str, object]:
        analysis = self.last_analysis or self.analyze()
        return {
            "type": "rise_fall_strategy_status",
            "state": self.state,
            "direction": self.direction,
            "observed_ticks": len(self.prices),
            "warmup_ticks": self.warmup_ticks,
            "signal_generated": self.signal_generated,
            "fast_ema": analysis.fast_ema,
            "slow_ema": analysis.slow_ema,
            "rsi": analysis.rsi,
            "support": analysis.support,
            "resistance": analysis.resistance,
            "reason": reason,
        }


def scan_rise_fall(prices: list[float], direction: str | None = None) -> str | None:
    """Analyze a price series and return a confirmed CALL/PUT signal."""
    directions = (direction,) if direction else ("rise", "fall")
    for candidate in directions:
        strategy = RiseFallStrategy(candidate)
        strategy.seed(prices)
        result = strategy.analyze().signal
        if result is not None:
            return result
    return None
