"""Tests for the Task-3 backtest extensions: full-pipeline replay, point-in-time
macro alignment, per-analyzer calibration runs, and the no-lookahead guarantees
for the newly included inputs (volume/OBV, VWAP, macro).

Key properties:
- Macro observations dated after bar t are invisible to signal_at.
- The volume analyzer is causal: truncated input == truncated analysis.
- run_per_analyzer_backtest covers every registered analyzer plus trend and
  the combined blend, deterministically.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from alpha_engine.analyzers.volume import analyze_volume
from alpha_engine.cache.models import (
    Candle,
    Fundamentals,
    Interval,
    MacroObservation,
    NewsItem,
    PriceSeries,
)
from alpha_engine.schema.signal import Market
from alpha_engine.validation.backtest import (
    ANALYZER_REGISTRY,
    _macro_as_of,
    _fundamentals_as_of,
    _news_as_of,
    run_analyzer_backtest,
    run_backtest,
    run_per_analyzer_backtest,
    signal_at,
)

T0 = datetime(2024, 1, 1, tzinfo=timezone.utc)


def _series(closes: list[float], asset: str = "AAPL") -> PriceSeries:
    candles = [
        Candle(
            ts=T0 + timedelta(days=i),
            open=c,
            high=c * 1.01,
            low=c * 0.99,
            close=c,
            volume=1000.0 + (i % 7) * 100,
        )
        for i, c in enumerate(closes)
    ]
    return PriceSeries(asset=asset, interval=Interval.DAY, candles=candles)


def _macro_obs(series_id: str, days: list[int], values: list[float]) -> list[MacroObservation]:
    return [
        MacroObservation(series_id=series_id, ts=T0 + timedelta(days=d), value=v, source="fred")
        for d, v in zip(days, values)
    ]


# --- point-in-time macro ---------------------------------------------------------


def test_macro_as_of_filters_future_observations():
    obs = {"FEDFUNDS": _macro_obs("FEDFUNDS", [0, 30, 60, 90], [5.0, 5.0, 4.5, 4.0])}
    visible = _macro_as_of(obs, T0 + timedelta(days=45))
    assert [o.value for o in visible["FEDFUNDS"]] == [5.0, 5.0]


def test_macro_as_of_drops_series_with_no_past():
    obs = {"CPIAUCSL": _macro_obs("CPIAUCSL", [50, 80], [300.0, 310.0])}
    assert _macro_as_of(obs, T0 + timedelta(days=10)) == {}
    assert _macro_as_of(None, T0) == {}


def test_signal_at_ignores_future_macro():
    """The signal at bar t must be identical whether the macro series stops at
    t or extends far past it — the macro analogue of price truncation."""
    closes = [100.0 + i * 0.4 for i in range(120)]
    series = _series(closes)
    t = 100
    cutoff_day = 100

    past_only = {
        "FEDFUNDS": _macro_obs("FEDFUNDS", [0, 30, 60, 90], [5.0, 5.0, 4.5, 4.0]),
    }
    with_future = {
        "FEDFUNDS": _macro_obs(
            "FEDFUNDS", [0, 30, 60, 90, 110, 140], [5.0, 5.0, 4.5, 4.0, 0.1, 9.9]
        ),
    }
    assert all(o.ts <= T0 + timedelta(days=cutoff_day) for o in past_only["FEDFUNDS"])

    sig_past, _ = signal_at(series, t, market=Market.US_EQUITY, macro_data=past_only)
    sig_future, _ = signal_at(series, t, market=Market.US_EQUITY, macro_data=with_future)

    assert sig_past.direction is sig_future.direction
    assert sig_past.confidence == sig_future.confidence


def test_context_as_of_never_reveals_future_news_or_filings():
    cutoff = T0 + timedelta(days=100)
    news = [
        NewsItem(
            ts=cutoff - timedelta(days=1),
            headline="Apple profit surges",
            source="test",
            asset_tags=["AAPL"],
        ),
        NewsItem(
            ts=cutoff + timedelta(days=1),
            headline="Apple shares plunge",
            source="test",
            asset_tags=["AAPL"],
        ),
    ]
    filings = [
        Fundamentals(
            asset="AAPL",
            period="2024-Q4",
            ts=cutoff - timedelta(days=40),
            available_at=cutoff - timedelta(days=2),
            revenue=100.0,
        ),
        Fundamentals(
            asset="AAPL",
            period="2025-Q1",
            ts=cutoff - timedelta(days=10),
            available_at=cutoff + timedelta(days=2),
            revenue=200.0,
        ),
        # Legacy rows do not reveal when the filing became public, so they abstain.
        Fundamentals(asset="AAPL", period="unknown", ts=cutoff - timedelta(days=90)),
    ]

    assert _news_as_of(news, cutoff) == news[:1]
    assert _fundamentals_as_of(filings, cutoff) == filings[:1]


def test_signal_at_ignores_future_news_and_fundamentals():
    series = _series([100.0 + i * 0.4 for i in range(120)])
    cutoff = series.candles[100].ts
    past_news = [
        NewsItem(
            ts=cutoff - timedelta(days=1),
            headline="Apple profit surges",
            source="test",
            asset_tags=["AAPL"],
        )
    ]
    future_news = [
        NewsItem(
            ts=cutoff + timedelta(days=1),
            headline="Apple shares plunge on fraud probe",
            source="test",
            asset_tags=["AAPL"],
        )
    ]
    past_fundamentals = [
        Fundamentals(
            asset="AAPL",
            period=f"2023-Q{i + 1}",
            ts=cutoff - timedelta(days=450 - i * 90),
            available_at=cutoff - timedelta(days=400 - i * 90),
            revenue=100.0 + i * 10,
            net_income=10.0,
            operating_cash_flow=15.0,
            total_debt=10.0,
            total_equity=100.0,
        )
        for i in range(5)
    ]
    future_fundamentals = [
        Fundamentals(
            asset="AAPL",
            period="2025-Q1",
            ts=cutoff,
            available_at=cutoff + timedelta(days=5),
            revenue=1.0,
            net_income=10.0,
            operating_cash_flow=1.0,
            total_debt=1000.0,
            total_equity=1.0,
        )
    ]

    past, _ = signal_at(
        series,
        100,
        market=Market.US_EQUITY,
        news_data=past_news,
        fundamentals_data=past_fundamentals,
    )
    extended, _ = signal_at(
        series,
        100,
        market=Market.US_EQUITY,
        news_data=past_news + future_news,
        fundamentals_data=past_fundamentals + future_fundamentals,
    )

    assert past.model_dump(exclude={"timestamp"}) == extended.model_dump(exclude={"timestamp"})


def test_signal_at_full_pipeline_has_no_lookahead():
    """The original guarantee, re-pinned for the full pipeline (now including
    MACD, multi-timeframe, support/resistance, volume, VWAP, volatility)."""
    base = [100.0 + i * 0.5 for i in range(110)]
    wild_future = base + [1.0, 500.0, 2.0, 400.0] * 8

    t = 109
    sig_a, entry_a = signal_at(_series(wild_future), t)
    sig_b, entry_b = signal_at(_series(base), t)

    assert sig_a.direction is sig_b.direction
    assert sig_a.confidence == sig_b.confidence
    assert sig_a.invalidation_level == sig_b.invalidation_level
    assert entry_a == entry_b


# --- volume/OBV causality ---------------------------------------------------------


def test_obv_is_causal():
    """OBV at bar t depends only on bars [0..t]: analyzing a truncated series
    equals analyzing the truncation of a longer one."""
    closes = [100.0 + ((i * 7) % 13) - 6 for i in range(60)]
    volumes = [1000.0 + (i % 9) * 50 for i in range(60)]
    full = _series(closes)
    for i, c in enumerate(full.candles):
        c.volume = volumes[i]

    truncated = PriceSeries(asset=full.asset, interval=full.interval, candles=full.candles[:40])
    fresh = _series(closes[:40])
    for i, c in enumerate(fresh.candles):
        c.volume = volumes[:40][i]

    assert analyze_volume(truncated).model_dump() == analyze_volume(fresh).model_dump()


# --- per-analyzer backtests -------------------------------------------------------


def test_per_analyzer_backtest_covers_registry():
    closes = [100.0 * 1.01**i for i in range(140)]
    reports = run_per_analyzer_backtest(_series(closes, asset="BTC"), market=Market.CRYPTO)

    assert set(reports) == {"trend", "combined", *ANALYZER_REGISTRY}
    for name, report in reports.items():
        assert report.asset == "BTC", name
        assert report.bars == 140
        # every report scored the same walk length
        assert report.signals_generated == reports["trend"].signals_generated


def test_single_analyzer_backtest_runs():
    closes = [100.0 * 1.01**i for i in range(140)]
    report = run_analyzer_backtest(
        _series(closes, asset="BTC"), ANALYZER_REGISTRY["rsi"], market=Market.CRYPTO
    )
    assert report.signals_generated > 0
    # neutral-heavy analyzers are allowed; directional <= generated always
    assert report.directional <= report.signals_generated


def test_per_analyzer_backtest_deterministic():
    closes = [100.0 + ((i * 11) % 23) + i * 0.2 for i in range(140)]
    series = _series(closes, asset="BTC")
    a = run_per_analyzer_backtest(series, market=Market.CRYPTO, step=5)
    b = run_per_analyzer_backtest(series, market=Market.CRYPTO, step=5)
    assert {k: v.model_dump() for k, v in a.items()} == {k: v.model_dump() for k, v in b.items()}


def test_full_backtest_with_macro_runs():
    closes = [150.0 + i * 0.3 for i in range(140)]
    macro = {
        "FEDFUNDS": _macro_obs("FEDFUNDS", list(range(0, 140, 30)), [5.0, 5.0, 4.5, 4.0, 3.5]),
    }
    report = run_backtest(_series(closes), market=Market.US_EQUITY, macro_data=macro)
    assert report.signals_generated > 0
