"""The research candidate must not see future filings or future bars."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from alpha_engine.cache.models import Candle, Fundamentals, Interval, PriceSeries
from alpha_engine.validation.momentum_candidate import (
    ASSETS,
    WARMUP,
    _account,
    _revenue_veto,
    evaluate,
)


def _candle(day, price):
    return Candle(ts=day, open=price, high=price, low=price, close=price)


def test_filing_gate_and_next_open_fill():
    past = Fundamentals(
        asset="AAPL",
        period="2025-Q1",
        ts=datetime(2025, 3, 31, tzinfo=timezone.utc),
        available_at=datetime(2025, 5, 1, tzinfo=timezone.utc),
        revenue=100,
    )
    latest = Fundamentals(
        asset="AAPL",
        period="2026-Q1",
        ts=datetime(2026, 3, 31, tzinfo=timezone.utc),
        available_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        revenue=80,
    )
    assert not _revenue_veto([past, latest], datetime(2026, 4, 30, tzinfo=timezone.utc))
    assert _revenue_veto([past, latest], datetime(2026, 5, 2, tzinfo=timezone.utc))
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    bars = {a: [_candle(start + timedelta(days=i), 100) for i in range(WARMUP + 2)] for a in ASSETS}
    bars["AAPL"][-1].open = 110
    bars["AAPL"][-1].close = 110
    target = {a: (0.5 if a == "AAPL" else 0.0) for a in ASSETS}
    # A close-time decision cannot profit from the following opening gap.
    assert _account(bars, [target], 0)["return_pct"] == 0

    bars = {a: [_candle(start + timedelta(days=i), 100) for i in range(WARMUP + 3)] for a in ASSETS}
    bars["AAPL"][-1].open = 200
    bars["AAPL"][-1].close = 300
    # One purchase then drift: no uncharged daily rebalance back to 50/50.
    assert _account(bars, [target, target], 0)["return_pct"] == 100


def test_holdout_bars_cannot_change_development_result():
    start = datetime(2025, 8, 1, tzinfo=timezone.utc)
    base = {
        a: PriceSeries(
            asset=a,
            interval=Interval.DAY,
            candles=[
                _candle(start + timedelta(days=i), 100 + i * (j + 1) / 10) for i in range(300)
            ],
        )
        for j, a in enumerate(ASSETS)
    }
    expected = evaluate(base, {})
    extended = {
        a: s.model_copy(
            update={
                "candles": s.candles + [_candle(datetime(2026, 9, 22, tzinfo=timezone.utc), 99999)]
            }
        )
        for a, s in base.items()
    }
    actual = evaluate(extended, {})
    assert actual["reserved_holdout_bars"] == 1
    for key in ("technical", "technical_fundamental", "equal_weight_buy_hold", "picks"):
        assert actual[key] == expected[key]
    with pytest.raises(ValueError, match="cost_bps"):
        evaluate(base, {}, cost_bps=-1)
