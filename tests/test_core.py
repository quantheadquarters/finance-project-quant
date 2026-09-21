"""Tests for the deterministic core. These prove the cardinal rule: given fixed
inputs, the analysis and synthesis layers produce fixed, predictable outputs. No
network, no LLM, no randomness. A reader running `pytest` sees the engine is real.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from alpha_engine.analyzers.crypto_trend import analyze_trend
from alpha_engine.cache.models import Candle, Interval, PriceSeries
from alpha_engine.schema.signal import (
    Direction,
    Market,
    Signal,
    SignalSource,
    Timeframe,
)
from alpha_engine.synthesis.synthesize import synthesize


def _series(closes: list[float]) -> PriceSeries:
    candles = [
        Candle(
            ts=datetime(2024, 1, 1, tzinfo=timezone.utc),
            open=c,
            high=c,
            low=c,
            close=c,
        )
        for c in closes
    ]
    return PriceSeries(asset="BTC", interval=Interval.DAY, candles=candles)


def test_schema_rejects_blank_asset():
    with pytest.raises(ValueError):
        Signal(
            asset="  ",
            market=Market.CRYPTO,
            direction=Direction.NEUTRAL,
            confidence=0.0,
            timeframe=Timeframe.SWING,
            invalidation_level=None,
            thesis="",
        )


def test_schema_requires_utc_timestamp():
    with pytest.raises(ValueError):
        Signal(
            asset="BTC",
            market=Market.CRYPTO,
            direction=Direction.NEUTRAL,
            confidence=0.0,
            timeframe=Timeframe.SWING,
            timestamp=datetime(2024, 1, 1),  # naive
            invalidation_level=None,
            thesis="",
        )


def test_confidence_bounds_enforced():
    with pytest.raises(ValueError):
        Signal(
            asset="BTC",
            market=Market.CRYPTO,
            direction=Direction.BULLISH,
            confidence=1.5,
            timeframe=Timeframe.SWING,
            invalidation_level=None,
            thesis="",
        )


def test_trend_rising_series_is_bullish():
    rising = [float(i) for i in range(1, 60)]
    src = analyze_trend(_series(rising))
    assert src.direction is Direction.BULLISH
    assert src.weight > 0


def test_trend_falling_series_is_bearish():
    falling = [float(i) for i in range(60, 1, -1)]
    src = analyze_trend(_series(falling))
    assert src.direction is Direction.BEARISH


def test_trend_insufficient_history_is_neutral():
    src = analyze_trend(_series([1.0, 2.0, 3.0]))
    assert src.direction is Direction.NEUTRAL
    assert src.weight == 0.0


def test_trend_is_deterministic():
    rising = [float(i) for i in range(1, 60)]
    a = analyze_trend(_series(rising))
    b = analyze_trend(_series(rising))
    assert a.model_dump() == b.model_dump()


def test_synthesis_single_bullish_source():
    src = SignalSource(name="t", direction=Direction.BULLISH, weight=0.8, detail="test")
    sig = synthesize("BTC", Market.CRYPTO, [src])
    assert sig.direction is Direction.BULLISH
    assert 0.0 <= sig.confidence <= 1.0


def test_synthesis_contradictory_sources_lower_confidence():
    bull = SignalSource(name="a", direction=Direction.BULLISH, weight=0.8, detail="test")
    bear = SignalSource(name="b", direction=Direction.BEARISH, weight=0.8, detail="test")
    sig = synthesize("BTC", Market.CRYPTO, [bull, bear])
    # equal and opposite -> neutral, near-zero confidence
    assert sig.direction is Direction.NEUTRAL
    assert sig.confidence < 0.2


def test_synthesis_empty_sources_is_neutral():
    sig = synthesize("BTC", Market.CRYPTO, [])
    assert sig.direction is Direction.NEUTRAL
    assert sig.confidence == 0.0


def test_equity_context_can_confirm_but_cannot_invent_or_reverse_price_direction():
    price = [
        SignalSource(name="trend", direction=Direction.BULLISH, weight=0.4),
        SignalSource(name="macd", direction=Direction.BEARISH, weight=0.4),
    ]
    bullish_context = SignalSource(name="fundamentals", direction=Direction.BULLISH, weight=0.35)
    assert (
        synthesize("AAPL", Market.US_EQUITY, price + [bullish_context]).direction
        is Direction.BULLISH
    )
    anchored = synthesize(
        "AAPL", Market.US_EQUITY, price + [bullish_context], primary_sources=price
    )
    assert anchored.direction is Direction.NEUTRAL
    assert anchored.confidence == 0.0

    bullish_price = [SignalSource(name="trend", direction=Direction.BULLISH, weight=0.4)]
    alone = synthesize("AAPL", Market.US_EQUITY, bullish_price, primary_sources=bullish_price)
    confirmed = synthesize(
        "AAPL",
        Market.US_EQUITY,
        bullish_price + [bullish_context],
        primary_sources=bullish_price,
    )
    assert confirmed.direction is Direction.BULLISH
    assert confirmed.confidence > alone.confidence

    bearish_price = [SignalSource(name="trend", direction=Direction.BEARISH, weight=0.2)]
    vetoed = synthesize(
        "AAPL", Market.US_EQUITY, bearish_price + [bullish_context], primary_sources=bearish_price
    )
    assert vetoed.direction is Direction.NEUTRAL


def test_confidence_lower_with_fewer_sources():
    """One source should produce lower confidence than three agreeing sources,
    even with the same agreement quality. This tests the source-count cap."""
    single = [SignalSource(name="rsi", direction=Direction.BULLISH, weight=0.8, detail="test")]
    triple = [
        SignalSource(name="rsi", direction=Direction.BULLISH, weight=0.8, detail="test"),
        SignalSource(name="bollinger", direction=Direction.BULLISH, weight=0.7, detail="test"),
        SignalSource(name="crypto.trend", direction=Direction.BULLISH, weight=0.9, detail="test"),
    ]
    sig1 = synthesize("BTC", Market.CRYPTO, single)
    sig3 = synthesize("BTC", Market.CRYPTO, triple)
    assert sig1.confidence < sig3.confidence


def test_confidence_lower_with_disagreement():
    """When sources disagree, confidence should drop even if the net direction
    is clear."""
    agree = [
        SignalSource(name="rsi", direction=Direction.BULLISH, weight=0.8, detail="test"),
        SignalSource(name="bollinger", direction=Direction.BULLISH, weight=0.7, detail="test"),
    ]
    disagree = [
        SignalSource(name="rsi", direction=Direction.BULLISH, weight=0.8, detail="test"),
        SignalSource(name="bollinger", direction=Direction.BEARISH, weight=0.7, detail="test"),
    ]
    sig_agree = synthesize("BTC", Market.CRYPTO, agree)
    sig_disagree = synthesize("BTC", Market.CRYPTO, disagree)
    # Both should resolve (one bullish, one likely bearish or neutral),
    # but the disagreeing pair should have lower confidence in its direction
    if sig_disagree.direction is Direction.NEUTRAL:
        assert sig_disagree.confidence < sig_agree.confidence
    else:
        assert sig_disagree.confidence < sig_agree.confidence


def test_confidence_never_exceeds_source_count_cap():
    """Confidence with 1 source should never exceed 0.45."""
    src = SignalSource(name="rsi", direction=Direction.BULLISH, weight=1.0, detail="")
    sig = synthesize("BTC", Market.CRYPTO, [src])
    assert sig.confidence <= 0.45


def test_confidence_is_deterministic():
    """Same inputs must always produce the same confidence."""
    sources = [
        SignalSource(name="rsi", direction=Direction.BULLISH, weight=0.8, detail="test"),
        SignalSource(name="bollinger", direction=Direction.BULLISH, weight=0.7, detail="test"),
    ]
    a = synthesize("BTC", Market.CRYPTO, sources)
    b = synthesize("BTC", Market.CRYPTO, sources)
    assert a.confidence == b.confidence
    assert a.direction == b.direction


# ---------------------------------------------------------------------------
# Conviction scalar — regression tests for a real bug
#
# The volatility and macro-calendar layers are documented as *reducing*
# confidence in an extreme tape or before a policy decision. They did so by
# multiplying every source weight by a constant — which turned out to do
# nothing at all, because every term in the confidence formula is a ratio and a
# constant factor cancels out of all of them. The dampening was visible in the
# audit trail and absent from the number.
# ---------------------------------------------------------------------------


def _scalar_sources():
    from alpha_engine.schema.signal import Direction, SignalSource

    return [
        SignalSource(name="equity.trend", direction=Direction.BULLISH, weight=0.5),
        SignalSource(name="rsi", direction=Direction.BULLISH, weight=0.4),
        SignalSource(name="macd", direction=Direction.BEARISH, weight=0.2),
    ]


def test_scaling_all_weights_does_not_change_confidence():
    """The bug itself, pinned so nobody 'fixes' dampening by scaling weights
    again and believes it worked."""
    from alpha_engine.schema.signal import Market
    from alpha_engine.synthesis.synthesize import synthesize

    srcs = _scalar_sources()
    full = synthesize("X", Market.US_EQUITY, srcs)
    scaled = synthesize(
        "X", Market.US_EQUITY, [s.model_copy(update={"weight": s.weight * 0.5}) for s in srcs]
    )
    assert full.confidence == scaled.confidence


def test_conviction_scalar_actually_reduces_confidence():
    from alpha_engine.schema.signal import Market
    from alpha_engine.synthesis.synthesize import synthesize

    srcs = _scalar_sources()
    full = synthesize("X", Market.US_EQUITY, srcs)
    damped = synthesize("X", Market.US_EQUITY, srcs, conviction_scalar=0.6)
    assert damped.confidence < full.confidence
    assert damped.confidence == pytest.approx(round(full.confidence * 0.6, 4))


def test_conviction_scalar_defaults_to_no_change():
    from alpha_engine.schema.signal import Market
    from alpha_engine.synthesis.synthesize import synthesize

    srcs = _scalar_sources()
    assert (
        synthesize("X", Market.US_EQUITY, srcs).confidence
        == synthesize("X", Market.US_EQUITY, srcs, conviction_scalar=1.0).confidence
    )


def test_conviction_scalar_can_never_raise_confidence():
    """These layers are defensive by construction. A scalar above 1.0 is a
    caller bug, and it must be clamped rather than amplifying conviction."""
    from alpha_engine.schema.signal import Market
    from alpha_engine.synthesis.synthesize import synthesize

    srcs = _scalar_sources()
    full = synthesize("X", Market.US_EQUITY, srcs)
    boosted = synthesize("X", Market.US_EQUITY, srcs, conviction_scalar=5.0)
    assert boosted.confidence == full.confidence


def test_conviction_scalar_of_zero_zeroes_confidence():
    from alpha_engine.schema.signal import Market
    from alpha_engine.synthesis.synthesize import synthesize

    assert (
        synthesize("X", Market.US_EQUITY, _scalar_sources(), conviction_scalar=0.0).confidence
        == 0.0
    )


def test_conviction_scalar_keeps_confidence_in_range():
    from alpha_engine.schema.signal import Market
    from alpha_engine.synthesis.synthesize import synthesize

    for scalar in (0.0, 0.25, 0.6, 1.0):
        c = synthesize(
            "X", Market.US_EQUITY, _scalar_sources(), conviction_scalar=scalar
        ).confidence
        assert 0.0 <= c <= 1.0
