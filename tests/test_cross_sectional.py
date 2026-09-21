from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from alpha_engine.cache.models import Candle, Interval, PriceSeries
from alpha_engine.quant.cross_sectional import evaluate_cross_sectional


def _persistent_trends() -> dict[str, PriceSeries]:
    """Synthetic stocks whose relative trends genuinely persist."""
    out = {}
    for asset_index in range(8):
        price = 100.0
        candles = []
        drift = (asset_index - 3.5) * 0.0008
        for day in range(320):
            price *= math.exp(drift + 0.001 * math.sin(day / 11 + asset_index))
            candles.append(
                Candle(
                    ts=datetime(2023, 1, 1, tzinfo=timezone.utc) + timedelta(days=day),
                    open=price,
                    high=price * 1.002,
                    low=price * 0.998,
                    close=price,
                    volume=1_000_000 + asset_index * 10_000,
                )
            )
        out[f"S{asset_index}"] = PriceSeries(
            asset=f"S{asset_index}", interval=Interval.DAY, candles=candles
        )
    return out


def test_walk_forward_model_finds_persistent_cross_sectional_structure() -> None:
    report = evaluate_cross_sectional(
        _persistent_trends(),
        factors=["mom_20"],
        horizon=10,
        min_train_dates=80,
        cost_bps=0,
        null_runs=10,
    )

    assert len(report.periods) >= 15
    assert report.total_return > report.shuffled_total_return
    assert report.mean_rank_ic is not None and report.mean_rank_ic > 0.8
    assert "still not alpha" in report.verdict
