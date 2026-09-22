"""The context experiment compares the same resolved calls, not separate denominators."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from alpha_engine.cache.models import Candle, Interval, NewsItem, PriceSeries
from alpha_engine.schema.signal import Direction
from alpha_engine.validation import compare_context as comparison


def test_context_comparison_pairs_dates_and_reports_missing_news(monkeypatch):
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    candles = [
        Candle(
            ts=start + timedelta(days=i),
            open=100 + i,
            high=100 + i,
            low=100 + i,
            close=100 + i,
        )
        for i in range(120)
    ]
    series = PriceSeries(asset="AAPL", interval=Interval.DAY, candles=candles)

    def fake_signal_at(series, t, *, market, **kwargs):
        direction = (
            Direction.NEUTRAL if t == 90 and "fundamentals_data" in kwargs else Direction.BULLISH
        )
        return SimpleNamespace(
            direction=direction, invalidation_level=None, signal_sources=[]
        ), candles[t].close

    monkeypatch.setattr(comparison, "signal_at", fake_signal_at)
    unrelated = NewsItem(ts=start, headline="Other", source="test", asset_tags=["MSFT"])
    future = NewsItem(
        ts=start + timedelta(days=500),
        headline="Apple profit rises",
        source="test",
        asset_tags=["AAPL"],
    )
    duplicate = NewsItem(
        ts=start + timedelta(days=80),
        headline="Apple profit rises",
        source="test",
        url="https://example.com/apple",
        asset_tags=["AAPL"],
    )
    report = comparison.compare_context(
        series, [], [unrelated, future, duplicate, duplicate], step=10, cost_bps=10
    )

    pair = report["comparisons"]["fundamentals_vs_technical"]
    assert len(pair["matched_dates"]) == 2
    assert pair["baseline_resolved_directional"] == 3
    assert pair["candidate_resolved_directional"] == 2
    assert pair["matched_resolved"] == 2
    assert report["news_comparison_available"] is False
    assert report["tagged_news_items"] == 1
    assert pair["baseline"]["avg_move_after_cost_proxy"] == round(
        pair["baseline"]["avg_captured_move"] - 0.002, 6
    )


def test_news_has_separate_matched_denominator_and_requires_active_vote(monkeypatch):
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    candles = [
        Candle(ts=start + timedelta(days=i), open=100 + i, high=100 + i, low=100 + i, close=100 + i)
        for i in range(120)
    ]
    series = PriceSeries(asset="AAPL", interval=Interval.DAY, candles=candles)
    news = [
        NewsItem(
            ts=start + timedelta(days=75),
            headline="Apple profit rises",
            source="test",
            asset_tags=["AAPL"],
        )
    ]

    def fake_signal_at(series, t, *, market, **kwargs):
        has_fundamentals = "fundamentals_data" in kwargs
        has_news = "news_data" in kwargs
        neutral = (has_fundamentals and t == 90) or (has_news and t == 100)
        sources = (
            [SimpleNamespace(name="news.sentiment", weight=0.2)] if has_news and t == 80 else []
        )
        return SimpleNamespace(
            direction=Direction.NEUTRAL if neutral else Direction.BULLISH,
            invalidation_level=None,
            signal_sources=sources,
        ), candles[t].close

    monkeypatch.setattr(comparison, "signal_at", fake_signal_at)
    report = comparison.compare_context(series, [], news)

    assert report["news_active_dates"] == 1
    assert report["news_comparison_available"] is True
    assert report["comparisons"]["fundamentals_vs_technical"]["matched_resolved"] == 2
    assert report["comparisons"]["news_vs_technical_fundamental"]["matched_resolved"] == 1
    assert report["comparisons"]["news_vs_technical_fundamental"]["context_active_matched"] == 1


def test_news_vote_only_on_unmatched_date_is_not_a_comparison(monkeypatch):
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    series = PriceSeries(
        asset="AAPL",
        interval=Interval.DAY,
        candles=[
            Candle(ts=start + timedelta(days=i), open=100, high=100, low=100, close=100)
            for i in range(120)
        ],
    )
    news = [
        NewsItem(
            ts=start + timedelta(days=80),
            headline="Apple profit",
            source="test",
            asset_tags=["AAPL"],
        )
    ]

    def fake_signal(series, t, *, market, **kwargs):
        with_news = "news_data" in kwargs
        sources = (
            [SimpleNamespace(name="news.sentiment", weight=0.2)] if with_news and t == 80 else []
        )
        direction = Direction.NEUTRAL if sources else Direction.BULLISH
        return SimpleNamespace(
            direction=direction, invalidation_level=None, signal_sources=sources
        ), 100.0

    monkeypatch.setattr(comparison, "signal_at", fake_signal)
    report = comparison.compare_context(series, [], news)
    pair = report["comparisons"]["news_vs_technical_fundamental"]
    assert report["news_active_dates"] == 1
    assert pair["matched_resolved"] == 2
    assert pair["context_active_matched"] == 0
    assert report["news_comparison_available"] is False


def test_real_pipeline_counts_scored_headline_as_active_news():
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    series = PriceSeries(
        asset="AAPL",
        interval=Interval.DAY,
        candles=[
            Candle(
                ts=start + timedelta(days=i),
                open=100 + i,
                high=100 + i,
                low=100 + i,
                close=100 + i,
                volume=1_000_000,
            )
            for i in range(120)
        ],
    )
    news = [
        NewsItem(
            ts=start + timedelta(days=80),
            headline="Apple profit surges",
            source="independent_test",
            asset_tags=["AAPL"],
        )
    ]

    report = comparison.compare_context(series, [], news)

    assert report["news_active_dates"] > 0
    assert report["news_comparison_available"] is True
    assert "news_vs_technical_fundamental" in report["comparisons"]


def test_context_comparison_rejects_invalid_settings():
    series = PriceSeries(asset="AAPL", interval=Interval.DAY, candles=[])
    with pytest.raises(ValueError, match="step must be positive"):
        comparison.compare_context(series, [], [], step=0)
    with pytest.raises(ValueError, match="not enough cached daily candles"):
        comparison.compare_context(series, [], [])


def test_apple_holdout_candles_cannot_change_development_result(monkeypatch):
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    candles = [
        Candle(
            ts=start + timedelta(days=i),
            open=100 + i,
            high=100 + i,
            low=100 + i,
            close=100 + i,
        )
        for i in range(130)
    ]

    def fake_signal_at(series, t, *, market, **kwargs):
        return SimpleNamespace(
            direction=Direction.BULLISH, invalidation_level=None, signal_sources=[]
        ), series.candles[t].close

    monkeypatch.setattr(comparison, "signal_at", fake_signal_at)
    original = PriceSeries(asset="AAPL", interval=Interval.DAY, candles=candles)
    changed = PriceSeries(
        asset="AAPL",
        interval=Interval.DAY,
        candles=[
            c.model_copy(update={"close": 10000.0})
            if c.ts.date() >= comparison.AAPL_HOLDOUT_START
            else c
            for c in candles
        ],
    )

    a = comparison.compare_context(original, [], [])
    b = comparison.compare_context(changed, [], [])
    assert a == b
    assert a["reserved_holdout_bars"] == 17
    assert a["last_bar"] < "2026-09-22"


def test_comparison_cli_loads_unpruned_news_archive(monkeypatch, tmp_path, capsys):
    item = NewsItem(
        ts=datetime(2026, 1, 1, tzinfo=timezone.utc),
        headline="Apple profit rises",
        source="finnhub",
        asset_tags=["AAPL"],
    )
    archive = tmp_path / "news.json"
    archive.write_text(
        json.dumps(
            {
                "asset": "AAPL",
                "provider": "finnhub",
                "requested_from": "2025-01-01",
                "requested_to": "2026-09-22",
                "retrieved_at": "2026-09-22T00:00:00+00:00",
                "items": [item.model_dump(mode="json")],
            }
        )
    )
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    series = PriceSeries(
        asset="AAPL",
        interval=Interval.DAY,
        candles=[
            Candle(ts=start + timedelta(days=i), open=100, high=100, low=100, close=100)
            for i in range(120)
        ],
    )

    class FakeCache:
        def get_price(self, asset, interval):
            return series, False

        def get_fundamentals(self, asset):
            return [], False

    monkeypatch.setattr(comparison, "Cache", FakeCache)
    monkeypatch.setattr(
        comparison,
        "compare_context",
        lambda series, fundamentals, news, **kwargs: {
            "headlines": len(news),
            "last_bar": series.candles[-1].ts.isoformat(),
        },
    )
    monkeypatch.setattr(sys, "argv", ["compare_context", "AAPL", "--news-file", str(archive)])

    assert comparison.main() == 0
    assert '"headlines": 1' in capsys.readouterr().out
    incomplete = json.loads(archive.read_text())
    incomplete["requested_from"] = "2025-05-01"
    archive.write_text(json.dumps(incomplete))
    with pytest.raises(SystemExit):
        comparison.main()
    assert "does not cover" in capsys.readouterr().err
    incomplete["requested_from"] = (start + timedelta(days=80)).date().isoformat()
    archive.write_text(json.dumps(incomplete))
    with pytest.raises(SystemExit):
        comparison.main()
    assert "news lookback" in capsys.readouterr().err
    incomplete["requested_from"] = "2025-01-01"
    incomplete["items"][0]["ts"] = "2024-12-31T00:00:00Z"
    archive.write_text(json.dumps(incomplete))
    with pytest.raises(SystemExit):
        comparison.main()
    assert "outside archive" in capsys.readouterr().err
    incomplete["items"][0]["ts"] = item.ts.isoformat()
    incomplete["retrieved_at"] = "2025-01-01T00:00:00+00:00"
    archive.write_text(json.dumps(incomplete))
    with pytest.raises(SystemExit):
        comparison.main()
    assert "precede retrieval" in capsys.readouterr().err
