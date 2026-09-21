"""Breakout entries above or below the prior Donchian channel."""

from __future__ import annotations

from alpha_engine.cache.models import Candle
from alpha_engine.strategy.base import BaseStrategy


class DonchianBreakout(BaseStrategy):
    name = "Donchian Breakout"
    description = "Long above the prior range high, short below the prior range low."
    params = {"lookback": 20}

    def generate_signals(self, candles: list[Candle]) -> list[int]:
        lookback = int(self.params["lookback"])
        if lookback < 2:
            raise ValueError("lookback must be at least 2")
        out = [0] * len(candles)
        for i in range(lookback, len(candles)):
            prior = candles[i - lookback : i]  # exclude today's bar: no self-confirming breakout
            if candles[i].close > max(c.high for c in prior):
                out[i] = 1
            elif candles[i].close < min(c.low for c in prior):
                out[i] = -1
        return out
