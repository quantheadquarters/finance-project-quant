"""Momentum entries that require above-average volume participation."""

from __future__ import annotations

from alpha_engine.cache.models import Candle
from alpha_engine.strategy.base import BaseStrategy


class MomentumVolume(BaseStrategy):
    name = "Momentum + Volume"
    description = "Follow medium-term momentum only when volume confirms participation."
    params = {"lookback": 20, "threshold_pct": 3.0}

    def generate_signals(self, candles: list[Candle]) -> list[int]:
        lookback = int(self.params["lookback"])
        threshold = float(self.params["threshold_pct"]) / 100.0
        if lookback < 2 or threshold < 0:
            raise ValueError("lookback must be at least 2 and threshold_pct non-negative")

        out = [0] * len(candles)
        previous = 0
        for i in range(lookback, len(candles)):
            window = candles[i - lookback + 1 : i + 1]
            volumes = [c.volume for c in window]
            if candles[i - lookback].close <= 0 or any(v is None for v in volumes):
                continue
            momentum = candles[i].close / candles[i - lookback].close - 1.0
            volume_confirmed = candles[i].volume > sum(float(v) for v in volumes) / lookback
            current = 1 if momentum >= threshold else -1 if momentum <= -threshold else 0
            if current and current != previous and volume_confirmed:
                out[i] = current
                previous = current
        return out
