"""The workbench shows evidence honestly without refreshing or recording data."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from alpha_engine.cache.interface import Cache, LocalStore
from alpha_engine.cache.models import Candle, Fundamentals, Interval, NewsItem, PriceSeries
from alpha_engine.toolkit import DISCLAIMER, call_tool
from alpha_engine.web.server import _PAGES


def test_research_uses_cached_asset_evidence_and_preserves_dates(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    cache = Cache(LocalStore(tmp_path))
    cache.put_price(
        PriceSeries(
            asset="AAPL",
            interval=Interval.DAY,
            fetched_at=now,
            candles=[
                Candle(ts=now - timedelta(days=2), open=100, high=103, low=99, close=102),
                Candle(ts=now - timedelta(days=1), open=102, high=105, low=101, close=104),
            ],
        )
    )
    cache.put_fundamentals(
        "AAPL",
        [
            Fundamentals(
                asset="AAPL",
                period="2026-Q1",
                ts=now - timedelta(days=90),
                available_at=now - timedelta(days=50),
                revenue=100,
                source="sec",
            )
        ],
    )
    cache.put_news(
        "test",
        [
            NewsItem(
                ts=now - timedelta(days=1),
                headline="Apple profit rises",
                source="test",
                asset_tags=["AAPL"],
            ),
            NewsItem(
                ts=now, headline="Other firm profit rises", source="test", asset_tags=["MSFT"]
            ),
        ],
    )
    monkeypatch.setattr("alpha_engine.toolkit._cache", lambda: cache)
    monkeypatch.setattr("alpha_engine.validation.recorder.read_records", lambda: [])

    result = call_tool("research", {"asset": "aapl", "days": 30})

    assert result["disclaimer"] == DISCLAIMER
    assert result["asset"] == "AAPL"
    assert len(result["price"]["candles"]) == 2
    assert result["fundamentals"]["periods"][0]["available_at"] is not None
    assert result["fundamentals"]["analysis"]["name"] == "fundamentals"
    assert [item["headline"] for item in result["news"]["items"]] == ["Apple profit rises"]
    assert result["news"]["items"][0]["lexicon_score"] > 0
    assert result["news"]["analysis"]["name"] == "news.sentiment"
    assert result["news"]["stale"]  # A fresh headline for another ticker cannot mask this gap.
    assert result["latest_recorded_signal"] is None


def test_research_missing_prices_is_clear_and_page_is_routed(tmp_path, monkeypatch):
    monkeypatch.setattr("alpha_engine.toolkit._cache", lambda: Cache(LocalStore(tmp_path)))
    result = call_tool("research", {"asset": "AAPL"})
    assert "no cached daily prices" in result["error"]
    assert "--no-record" in result["hint"]
    assert result["disclaimer"] == DISCLAIMER
    assert _PAGES["/research"] == "research.html"


def test_news_shared_across_buckets_is_counted_once(tmp_path):
    cache = Cache(LocalStore(tmp_path))
    item = NewsItem(
        ts=datetime.now(timezone.utc),
        headline="Apple profit rises",
        source="finnhub",
        url="https://example.com/apple",
        asset_tags=["AAPL"],
    )
    cache.put_news("finnhub", [item])
    cache.put_news("finnhub_AAPL", [item])

    news, _ = cache.get_news()
    assert len(news) == 1


def test_strategy_result_has_matched_benchmark_and_data_fingerprint(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    cache = Cache(LocalStore(tmp_path))
    candles = [
        Candle(
            ts=now - timedelta(days=65 - i),
            open=100 + i,
            high=101 + i,
            low=99 + i,
            close=100 + i,
        )
        for i in range(65)
    ]
    cache.put_price(
        PriceSeries(
            asset="AAPL",
            interval=Interval.DAY,
            fetched_at=now,
            candles=candles,
            source="test-provider",
            currency="USD",
        )
    )
    monkeypatch.setattr("alpha_engine.toolkit._cache", lambda: cache)

    args = {"asset": "AAPL", "strategy": "SMACrossover", "include_series": True}
    first = call_tool("strategy_backtest", args)
    second = call_tool("strategy_backtest", args)

    assert first["experiment"]["price_source"] == "test-provider"
    assert first["experiment"]["currency"] == "USD"
    assert len(first["experiment"]["price_snapshot"]["candles"]) == first["bars"]
    assert len(first["experiment"]["strategy_source_sha256"]) == 64
    assert (
        first["experiment"]["price_snapshot_sha256"]
        == second["experiment"]["price_snapshot_sha256"]
    )
    assert len(first["benchmarks"]["buy_and_hold"]["equity_curve"]) == first["bars"]
    assert first["benchmarks"]["buy_and_hold"]["equity_curve"][0] == 100_000
    assert len(first["benchmarks"]["trailing_volatility"]["equity_curve"]) == first["bars"]
    assert first["benchmarks"]["trailing_volatility"]["annual_target_volatility_pct"] == 10
    assert "Hindsight" in first["benchmarks"]["risk_matched"]["note"]

    candles[-1].close += 1
    cache.put_price(
        PriceSeries(
            asset="AAPL",
            interval=Interval.DAY,
            fetched_at=now,
            candles=candles,
            source="test-provider",
            currency="USD",
        )
    )
    changed = call_tool("strategy_backtest", args)
    assert (
        changed["experiment"]["price_snapshot_sha256"]
        != first["experiment"]["price_snapshot_sha256"]
    )

    compact = call_tool("strategy_backtest", {**args, "include_series": False})
    assert "equity_curve" not in compact["benchmarks"]["buy_and_hold"]
    assert "equity_curve" not in compact["benchmarks"]["trailing_volatility"]
    assert "price_snapshot" not in compact["experiment"]


def test_offline_demo_is_explicitly_synthetic(tmp_path, monkeypatch):
    import runpy
    from pathlib import Path

    monkeypatch.setenv("ALPHA_DATA_DIR", str(tmp_path))
    script = Path(__file__).resolve().parents[1] / "scripts" / "load_demo.py"
    runpy.run_path(str(script))["main"]()

    series, _ = Cache().get_price("DEMO", "1d")
    assert series is not None
    assert len(series.candles) == 260
    assert series.source == "synthetic-demo"
    assert series.adjustment_policy == "synthetic"
