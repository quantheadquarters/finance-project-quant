"""Fair comparisons start on the same bar and carry reproducible inputs."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from alpha_engine.cache.models import Candle, Interval
from alpha_engine.strategy.benchmark import compare_with_buy_hold, trailing_volatility_baseline


def _candles() -> list[Candle]:
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    return [
        Candle(ts=start + timedelta(days=i), open=o, high=c, low=o, close=c)
        for i, (o, c) in enumerate([(100, 100), (110, 121), (121, 133.1)])
    ]


def test_buy_hold_enters_next_open_and_pays_entry_cost():
    result = compare_with_buy_hold(_candles(), [1000, 1000, 1000], Interval.DAY, 1000, 100)
    assert result["buy_and_hold"]["equity_curve"] == [1000, 1089, 1197.9]
    assert result["buy_and_hold"]["total_return_pct"] == 19.79
    assert result["risk_matched"]["stock_fraction"] == 0
    assert result["trailing_volatility"]["available"] is False
    assert "Insufficient history" in result["trailing_volatility"]["note"]


def test_risk_comparison_is_labeled_hindsight_and_cannot_lever():
    result = compare_with_buy_hold(_candles(), [1000, 500, 2000], Interval.DAY, 1000, 0)
    assert result["risk_matched"]["stock_fraction"] <= 1
    assert "Hindsight" in result["risk_matched"]["note"]


def test_trailing_baseline_uses_only_past_closes_and_charges_turnover():
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    candles = [
        Candle(ts=start + timedelta(days=i), open=100 + i, high=101 + i, low=100 + i, close=101 + i)
        for i in range(25)
    ]
    free = trailing_volatility_baseline(candles, Interval.DAY, 1000, 0)
    costly = trailing_volatility_baseline(candles, Interval.DAY, 1000, 100)
    assert free["equity_curve"][:21] == [1000] * 21
    assert free["available"] is True
    assert costly["total_return_pct"] < free["total_return_pct"]
    assert free["turnover"] > 0
    changed = candles[:-1] + [
        Candle(ts=candles[-1].ts, open=candles[-1].open, high=200, low=candles[-1].low, close=200)
    ]
    # The allocation for the final open uses history through the preceding close.
    assert (
        trailing_volatility_baseline(changed, Interval.DAY, 1000, 0)["turnover"] == free["turnover"]
    )
