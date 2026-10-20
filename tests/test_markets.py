"""Tests for Phase 2: the US equity path and macro context. Parsing rules for
the new ingestion adapters are pinned offline (pure functions, fixture
payloads); the analyzers are pinned as deterministic pure functions; and the
multi-source synthesis seam is exercised with trend + macro together for the
first time.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from alpha_engine.analyzers.crypto_trend import analyze_trend
from alpha_engine.analyzers.macro_context import MAX_WEIGHT, analyze_macro
from alpha_engine.cache.models import Candle, Interval, MacroObservation, PriceSeries
from alpha_engine.cli.main import detect_market
from alpha_engine.ingestion.fred import _parse_observations
from alpha_engine.ingestion.yahoo import _parse_chart
from alpha_engine.schema.signal import Direction, Market, SignalSource
from alpha_engine.synthesis.synthesize import synthesize

T0 = datetime(2020, 1, 1, tzinfo=timezone.utc)


def _series(closes: list[float], asset: str = "AAPL") -> PriceSeries:
    candles = [
        Candle(ts=T0 + timedelta(days=i), open=c, high=c, low=c, close=c)
        for i, c in enumerate(closes)
    ]
    return PriceSeries(asset=asset, interval=Interval.DAY, candles=candles)


def _monthly(series_id: str, values: list[float]) -> list[MacroObservation]:
    return [
        MacroObservation(
            series_id=series_id,
            ts=T0 + timedelta(days=31 * i),
            value=v,
            source="fred",
        )
        for i, v in enumerate(values)
    ]


# --- yahoo parsing ------------------------------------------------------------


def _yahoo_payload(timestamps, closes, error=None, **quote_overrides):
    quote = {
        "open": closes,
        "high": closes,
        "low": closes,
        "close": closes,
        "volume": [100] * len(closes),
    }
    quote.update(quote_overrides)
    return {
        "chart": {
            "error": error,
            "result": [{"timestamp": timestamps, "indicators": {"quote": [quote]}}],
        }
    }


def test_yahoo_parse_normalizes_bars():
    candles = _parse_chart(_yahoo_payload([1577836800, 1577923200], [300.0, 301.5]))
    assert len(candles) == 2
    assert candles[0].close == 300.0
    assert candles[0].ts.tzinfo is not None
    assert candles[1].volume == 100


def test_yahoo_parse_drops_null_close_bars():
    candles = _parse_chart(_yahoo_payload([1, 2, 3], [300.0, None, 302.0]))
    assert [c.close for c in candles] == [300.0, 302.0]


def test_yahoo_parse_fills_null_ohl_from_close():
    payload = _yahoo_payload([1], [300.0], open=[None], high=[None], low=[None])
    (candle,) = _parse_chart(payload)
    assert candle.open == candle.high == candle.low == 300.0


def test_yahoo_parse_raises_on_error_payload():
    with pytest.raises(ValueError, match="Not Found"):
        _parse_chart(_yahoo_payload([], [], error={"description": "Not Found"}))


def test_yahoo_parse_raises_on_empty_result():
    with pytest.raises(ValueError):
        _parse_chart({"chart": {"error": None, "result": []}})


def test_yahoo_fetch_records_provider_and_reported_currency(monkeypatch, tmp_path):
    from alpha_engine.cache.interface import Cache, LocalStore
    from alpha_engine.ingestion import yahoo

    payload = _yahoo_payload([1577836800, 1577923200], [300.0, 301.5])
    payload["chart"]["result"][0]["meta"] = {"currency": "USD"}

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return payload

    monkeypatch.setattr(yahoo.net, "get_with_retry", lambda *args, **kwargs: Response())
    series = yahoo.fetch_daily("AAPL", cache=Cache(LocalStore(tmp_path)))
    assert (series.source, series.currency, series.adjustment_policy) == (
        "yahoo",
        "USD",
        "unverified",
    )


# --- fred parsing -------------------------------------------------------------


def test_fred_parse_normalizes_and_skips_missing():
    payload = {
        "observations": [
            {"date": "2024-01-01", "value": "3.1"},
            {"date": "2024-02-01", "value": "."},  # FRED's missing marker
            {"date": "2024-03-01", "value": "3.4"},
        ]
    }
    obs = _parse_observations("CPIAUCSL", payload)
    assert [o.value for o in obs] == [3.1, 3.4]
    assert all(o.series_id == "CPIAUCSL" and o.source == "fred" for o in obs)
    assert obs[0].ts.tzinfo is not None


def test_fred_request_error_never_prints_api_key(monkeypatch, tmp_path):
    from alpha_engine.cache.interface import Cache, LocalStore
    from alpha_engine.ingestion import fred

    secret = "test-secret-token"

    class FailedResponse:
        def raise_for_status(self):
            raise RuntimeError(f"HTTP 403 for https://api.stlouisfed.org/?api_key={secret}")

    monkeypatch.setattr(fred.net, "get", lambda *args, **kwargs: FailedResponse())
    with pytest.raises(RuntimeError) as error:
        fred.fetch_series("FEDFUNDS", api_key=secret, cache=Cache(LocalStore(tmp_path)))
    assert secret not in str(error.value)
    assert "[REDACTED]" in str(error.value)


# --- equity trend -------------------------------------------------------------


def test_equity_trend_rising_is_bullish_with_own_name():
    src = analyze_trend(_series([float(i) for i in range(1, 60)]), name="equity.trend")
    assert src.name == "equity.trend"
    assert src.direction is Direction.BULLISH
    assert src.weight > 0


def test_equity_trend_numbers_match_crypto_delegate():
    # Pins today's deliberate delegation. When equity logic diverges, this test
    # should be updated to pin the divergence instead.
    series = _series([100.0 + ((i * 3) % 7) + i * 0.2 for i in range(60)])
    equity = analyze_trend(series, name="equity.trend")
    crypto = analyze_trend(series)
    assert equity.weight == crypto.weight
    assert equity.direction is crypto.direction


# --- macro context ------------------------------------------------------------


def test_macro_easing_and_cool_inflation_is_bullish_tilt():
    data = {
        "FEDFUNDS": _monthly("FEDFUNDS", [5.0, 5.0, 4.75, 4.5, 4.25, 4.0, 3.75]),  # cutting
        "CPIAUCSL": _monthly("CPIAUCSL", [100 + i * 0.15 for i in range(14)]),  # ~2% yoy
        "UNRATE": _monthly("UNRATE", [4.0, 4.0, 3.9, 3.8, 3.7, 3.6, 3.5]),  # improving
    }
    src = analyze_macro(data)
    assert src.name == "macro.context"
    assert src.direction is Direction.BULLISH
    assert 0 < src.weight <= MAX_WEIGHT


def test_macro_tightening_and_hot_inflation_is_bearish_tilt():
    data = {
        "FEDFUNDS": _monthly("FEDFUNDS", [1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0]),  # hiking
        "CPIAUCSL": _monthly("CPIAUCSL", [100 * (1.005**i) for i in range(14)]),  # ~6% yoy
        "UNRATE": _monthly("UNRATE", [3.5, 3.5, 3.6, 3.7, 3.8, 3.9, 4.0]),  # cracking
    }
    src = analyze_macro(data)
    assert src.direction is Direction.BEARISH
    assert src.weight == MAX_WEIGHT  # unanimous votes hit the cap exactly


def test_macro_missing_series_degrade_not_crash():
    only_ff = {"FEDFUNDS": _monthly("FEDFUNDS", [5.0, 4.75, 4.5, 4.25, 4.0, 3.75, 3.5])}
    src = analyze_macro(only_ff)
    assert src.direction is Direction.BULLISH  # one vote still counts
    assert src.weight <= MAX_WEIGHT

    assert analyze_macro({}).direction is Direction.NEUTRAL
    assert analyze_macro({}).weight == 0.0


def test_macro_is_deterministic():
    data = {"UNRATE": _monthly("UNRATE", [3.5, 3.6, 3.7, 3.8, 3.9, 4.0, 4.1])}
    assert analyze_macro(data).model_dump() == analyze_macro(data).model_dump()


# --- multi-source synthesis (the seam earning its keep) -----------------------


def test_synthesis_blends_trend_and_macro():
    trend = SignalSource(
        name="equity.trend", direction=Direction.BULLISH, weight=0.6, detail="test"
    )
    macro = SignalSource(
        name="macro.context", direction=Direction.BEARISH, weight=0.3, detail="test"
    )

    blended = synthesize("AAPL", Market.US_EQUITY, [trend, macro])
    trend_only = synthesize("AAPL", Market.US_EQUITY, [trend])

    # A contradicting macro tilt must temper conviction, never flip a strong trend.
    assert blended.direction is Direction.BULLISH
    assert blended.confidence < trend_only.confidence
    assert {s.name for s in blended.signal_sources} == {"equity.trend", "macro.context"}


def test_synthesis_agreeing_macro_raises_confidence():
    trend = SignalSource(
        name="equity.trend", direction=Direction.BULLISH, weight=0.6, detail="test"
    )
    macro = SignalSource(
        name="macro.context", direction=Direction.BULLISH, weight=0.3, detail="test"
    )
    blended = synthesize("AAPL", Market.US_EQUITY, [trend, macro])
    contradicted = synthesize(
        "AAPL",
        Market.US_EQUITY,
        [
            trend,
            SignalSource(
                name="macro.context", direction=Direction.BEARISH, weight=0.3, detail="test"
            ),
        ],
    )
    assert blended.confidence > contradicted.confidence


# --- market detection ----------------------------------------------------------


def test_market_autodetection():
    assert detect_market("BTC") is Market.CRYPTO
    assert detect_market("eth") is Market.CRYPTO
    assert detect_market("AAPL") is Market.US_EQUITY
    assert detect_market("AAPL", override="crypto") is Market.CRYPTO
