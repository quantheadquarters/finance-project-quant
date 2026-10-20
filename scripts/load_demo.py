"""Populate a clearly synthetic, network-free stock example for the workbench."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from alpha_engine.cache.interface import Cache
from alpha_engine.cache.models import Candle, Interval, PriceSeries


def main() -> None:
    today = datetime.now(timezone.utc).replace(hour=13, minute=30, second=0, microsecond=0)
    candles = []
    for i in range(260):
        baseline = 100 + 0.12 * i + 7 * math.sin(i / 11)
        opening = baseline - 0.6 * math.sin(i / 3)
        closing = baseline + 0.6 * math.sin(i / 3)
        candles.append(
            Candle(
                ts=today - timedelta(days=259 - i),
                open=round(opening, 4),
                high=round(max(opening, closing) + 1, 4),
                low=round(min(opening, closing) - 1, 4),
                close=round(closing, 4),
                volume=1000 + (i % 17) * 40,
            )
        )
    Cache().put_price(
        PriceSeries(
            asset="DEMO",
            interval=Interval.DAY,
            candles=candles,
            source="synthetic-demo",
            currency="USD",
            adjustment_policy="synthetic",
        )
    )
    print("DEMO: 260 synthetic candles cached. Open /research and enter DEMO. Not market data.")


if __name__ == "__main__":
    main()
