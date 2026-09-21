"""Trend-following entries when the existing Supertrend direction flips."""

from __future__ import annotations

from alpha_engine.cache.models import Candle
from alpha_engine.strategy.base import BaseStrategy
from alpha_engine.strategy.indicators import supertrend


class SupertrendFlip(BaseStrategy):
    name = "Supertrend Flip"
    description = "Long on a bullish Supertrend flip, short on a bearish flip."
    params = {"period": 10, "multiplier": 3.0}

    def generate_signals(self, candles: list[Candle]) -> list[int]:
        _, direction = supertrend(
            candles,
            period=int(self.params["period"]),
            multiplier=float(self.params["multiplier"]),
        )
        out = [0] * len(candles)
        for i in range(1, len(candles)):
            if direction[i] is not None and direction[i - 1] is not None:
                if direction[i] != direction[i - 1]:
                    out[i] = int(direction[i])
        return out
