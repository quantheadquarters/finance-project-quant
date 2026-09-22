"""The engine-signal trade replay must pay the next open, not a known close."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from alpha_engine.cache.models import Candle, Interval, NewsItem, PriceSeries
from alpha_engine.schema.signal import Direction
from alpha_engine.validation import trade_experiment


def _series(*, gap: bool = False) -> PriceSeries:
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    candles = []
    for i in range(84):
        opening = 120.0 if gap and i >= 81 else 100.0
        candles.append(
            Candle(
                ts=start + timedelta(days=i),
                open=opening,
                high=max(opening, 100.0),
                low=min(opening, 100.0),
                close=opening,
            )
        )
    return PriceSeries(asset="AAPL", interval=Interval.DAY, candles=candles)


def test_next_open_replay_does_not_capture_pre_fill_gap(monkeypatch):
    def fake_signal(series, t, *, market, **kwargs):
        direction = Direction.BULLISH if t == 80 else Direction.NEUTRAL
        return SimpleNamespace(direction=direction, signal_sources=[]), 100.0

    monkeypatch.setattr(trade_experiment, "signal_at", fake_signal)
    report = trade_experiment.run_trade_experiment(_series(gap=True), cost_bps=0)
    assert report["modes"]["technical"]["return_pct"] == 0.0
    assert report["modes"]["technical"]["position_changes"] == 2


def test_neutral_flattens_and_round_trip_pays_both_sides(monkeypatch):
    def fake_signal(series, t, *, market, **kwargs):
        direction = Direction.BULLISH if t == 80 else Direction.NEUTRAL
        return SimpleNamespace(direction=direction, signal_sources=[]), 100.0

    monkeypatch.setattr(trade_experiment, "signal_at", fake_signal)
    report = trade_experiment.run_trade_experiment(_series(), cost_bps=10)
    assert report["modes"]["technical"]["return_pct"] == -0.2
    assert report["modes"]["technical"]["active_intervals"] == 1


def test_holdout_candles_never_reach_trade_signals(monkeypatch):
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    candles = [
        Candle(ts=start + timedelta(days=i), open=100, high=100, low=100, close=100)
        for i in range(130)
    ]
    seen = []

    def fake_signal(series, t, *, market, **kwargs):
        seen.append(series.candles[-1].ts.date())
        return SimpleNamespace(direction=Direction.NEUTRAL, signal_sources=[]), 100.0

    monkeypatch.setattr(trade_experiment, "signal_at", fake_signal)
    report = trade_experiment.run_trade_experiment(
        PriceSeries(asset="AAPL", interval=Interval.DAY, candles=candles)
    )
    assert report["reserved_holdout_bars"] == 17
    assert max(seen) < trade_experiment.AAPL_HOLDOUT_START


def test_news_archive_duplicates_cannot_multiply_sentiment(monkeypatch):
    seen = []

    def fake_signal(series, t, *, market, **kwargs):
        if "news_data" in kwargs:
            seen.append(len(kwargs["news_data"]))
        sources = [SimpleNamespace(name="news.sentiment", weight=0.2)] if seen else []
        return SimpleNamespace(direction=Direction.NEUTRAL, signal_sources=sources), 100.0

    monkeypatch.setattr(trade_experiment, "signal_at", fake_signal)
    item = NewsItem(
        ts=datetime(2025, 3, 22, tzinfo=timezone.utc),
        headline="Apple profit surges",
        source="test",
        url="https://example.com/apple",
        asset_tags=["AAPL"],
    )
    report = trade_experiment.run_trade_experiment(_series(), news=[item, item])
    assert set(seen) == {1}
    assert report["news_active_closes"] > 0


def test_entry_fee_is_paid_before_market_move(monkeypatch):
    def fake_signal(series, t, *, market, **kwargs):
        direction = Direction.BULLISH if t == 80 else Direction.NEUTRAL
        return SimpleNamespace(direction=direction, signal_sources=[]), 100.0

    monkeypatch.setattr(trade_experiment, "signal_at", fake_signal)
    original = _series()
    candles = [
        c.model_copy(update={"open": 120.0, "high": 120.0, "low": 100.0, "close": 120.0})
        if i >= 82
        else c
        for i, c in enumerate(original.candles)
    ]
    report = trade_experiment.run_trade_experiment(
        original.model_copy(update={"candles": candles}), cost_bps=10
    )
    assert report["modes"]["technical"]["return_pct"] == 19.76


def test_replay_refuses_invalid_or_unordered_price_bars():
    original = _series()
    candles = list(original.candles)
    candles[81] = candles[81].model_copy(update={"high": 90.0})
    with pytest.raises(ValueError, match="valid OHLC"):
        trade_experiment.run_trade_experiment(original.model_copy(update={"candles": candles}))
    candles = list(original.candles)
    candles[81] = candles[81].model_copy(update={"ts": candles[80].ts})
    with pytest.raises(ValueError, match="ordered unique"):
        trade_experiment.run_trade_experiment(original.model_copy(update={"candles": candles}))


def test_negative_fee_and_market_factors_cannot_revive_account(monkeypatch):
    def fake_signal(series, t, *, market, **kwargs):
        direction = Direction.BULLISH if t == 80 else Direction.BEARISH
        return SimpleNamespace(direction=direction, signal_sources=[]), 100.0

    monkeypatch.setattr(trade_experiment, "signal_at", fake_signal)
    original = _series()
    candles = list(original.candles)
    candles[83] = candles[83].model_copy(update={"open": 300.0, "high": 300.0, "close": 300.0})
    report = trade_experiment.run_trade_experiment(
        original.model_copy(update={"candles": candles}), cost_bps=6000
    )
    assert report["modes"]["technical"]["return_pct"] == -100.0
