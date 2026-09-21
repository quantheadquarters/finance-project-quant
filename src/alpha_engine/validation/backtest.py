"""No-lookahead backtesting: replay a cached price series through the analyzers
as if each historical bar were "now", then score every generated signal against
only the bars that came after it.

The most common backtesting bug is lookahead — letting the simulated past peek
at the future through a full-series indicator, an off-by-one slice, or an
invalidation level computed on later data. The guard here is structural:
`signal_at(series, t)` is the ONLY way the backtester generates a signal, and it
truncates the series to bars [0..t] before any analysis runs. A unit test pins
the guarantee by asserting the signal at bar t is identical whether or not the
future exists in the input series. The same truncation applies to macro data:
observations dated after bar t are invisible to the simulated scan.

Two honest caveats, documented rather than hidden:
- Macro point-in-time uses each observation's own date. FRED silently revises
  history (CPI gets restated), so this is "the value as known today, dated to
  its period" — not a true archival vintage. Good enough to kill the gross
  lookahead; not a substitute for ALFRED vintages.
- Adjacent daily signals are heavily correlated; `step` thins the walk for a
  less flattering, more honest read.

Expect the finding that scaffold analyzers have little or no edge. That is the
point of this module: improvement happens against measured truth, not vibes.
"""

from __future__ import annotations

from collections.abc import Callable

from pydantic import BaseModel

from alpha_engine.analyzers.bollinger import analyze_bollinger
from alpha_engine.analyzers.crypto_trend import analyze_trend, trend_invalidation
from alpha_engine.analyzers.forex_trend import analyze_forex_trend
from alpha_engine.analyzers.fundamentals import analyze_fundamentals
from alpha_engine.analyzers.macd import analyze_macd
from alpha_engine.analyzers.macro_context import analyze_macro
from alpha_engine.analyzers.multi_timeframe import analyze_multi_timeframe
from alpha_engine.analyzers.rsi import analyze_rsi
from alpha_engine.analyzers.sentiment import analyze_sentiment
from alpha_engine.analyzers.support_resistance import analyze_support_resistance
from alpha_engine.analyzers.volatility import analyze_volatility, volatility_scalar
from alpha_engine.analyzers.volume import analyze_volume
from alpha_engine.analyzers.vwap import analyze_vwap
from alpha_engine.cache.models import Fundamentals, MacroObservation, NewsItem, PriceSeries
from alpha_engine.schema.signal import Direction, Market, Signal, SignalSource, Timeframe
from alpha_engine.synthesis.synthesize import synthesize
from alpha_engine.validation.outcomes import (
    HORIZON_BARS,
    Outcome,
    OutcomeSummary,
    score_forward,
    summarize_outcomes,
)

# Bars the slowest input needs before it is defined: the multi-timeframe long
# horizon (2 x 40) is now the binding constraint, plus margin.
DEFAULT_WARMUP = 80

# Which price-structure analyzer anchors each market.
_TREND_ANALYZER = {
    Market.CRYPTO: analyze_trend,
    Market.US_EQUITY: lambda s: analyze_trend(s, name="equity.trend"),
    Market.FOREX: analyze_forex_trend,
}

# Single-analyzer registry for per-analyzer calibration runs. Each entry is a
# pure function PriceSeries -> SignalSource; the trend anchor is added per
# market at runtime.
ANALYZER_REGISTRY: dict[str, Callable[[PriceSeries], SignalSource]] = {
    "rsi": analyze_rsi,
    "macd": analyze_macd,
    "bollinger": analyze_bollinger,
    "volume": analyze_volume,
    "vwap": analyze_vwap,
    "multi_timeframe": analyze_multi_timeframe,
    "support_resistance": analyze_support_resistance,
}


class BacktestReport(BaseModel):
    """The honest result of one backtest run. `signals_generated` counts every
    bar simulated; `directional` excludes neutrals (which are unscorable by
    definition); `summary` holds hit rate, average captured move, and the
    calibration curve over the directional signals."""

    asset: str
    market: Market
    timeframe: Timeframe
    bars: int
    warmup: int
    signals_generated: int
    directional: int
    summary: OutcomeSummary


def _macro_as_of(
    macro_data: dict[str, list[MacroObservation]] | None,
    cutoff_ts,
) -> dict[str, list[MacroObservation]]:
    """Point-in-time view of macro data: only observations dated on or before
    the simulated bar. This is the macro analogue of the price truncation."""
    if not macro_data:
        return {}
    visible: dict[str, list[MacroObservation]] = {}
    for series_id, obs in macro_data.items():
        past = [o for o in obs if o.ts <= cutoff_ts]
        if past:
            visible[series_id] = past
    return visible


def _news_as_of(items: list[NewsItem] | None, cutoff_ts) -> list[NewsItem]:
    """Headlines published by the simulated bar, never future news."""
    return [item for item in items or [] if item.ts <= cutoff_ts]


def _fundamentals_as_of(periods: list[Fundamentals] | None, cutoff_ts) -> list[Fundamentals]:
    """Filings public by the simulated bar.

    Old cache rows without ``available_at`` abstain. Treating their period-end
    date as publication would reveal earnings weeks before investors saw them.
    """
    return [p for p in periods or [] if p.available_at is not None and p.available_at <= cutoff_ts]


def signal_at(
    series: PriceSeries,
    t: int,
    market: Market = Market.CRYPTO,
    timeframe: Timeframe = Timeframe.SWING,
    macro_data: dict[str, list[MacroObservation]] | None = None,
    news_data: list[NewsItem] | None = None,
    fundamentals_data: list[Fundamentals] | None = None,
) -> tuple[Signal, float]:
    """Generate the signal the engine WOULD have emitted at bar index t, seeing
    only bars [0..t] (and only macro observations dated up to bar t). Returns
    (signal, entry_price at bar t's close).

    This is the no-lookahead choke point: the truncation happens here, before
    any analysis, so no caller can accidentally leak the future in.

    Replays the FULL live pipeline — trend anchor, RSI, MACD, Bollinger,
    multi-horizon alignment, support/resistance, volume, VWAP, optional macro,
    and the volatility-regime dampener — so the backtest measures what `scan`
    actually ships, not a simplified cousin.
    """
    from alpha_engine.analyzers.indian_equity import analyze_indian_equity

    visible = series.candles[: t + 1]
    past = PriceSeries(asset=series.asset, interval=series.interval, candles=visible)

    sources: list[SignalSource] = []
    if market is Market.CRYPTO:
        sources.append(analyze_trend(past))
    elif market is Market.IN_EQUITY:
        sources.append(analyze_indian_equity(past))
    elif market is Market.FOREX:
        sources.append(analyze_forex_trend(past))
    else:
        sources.append(analyze_trend(past, name="equity.trend"))

    sources.append(analyze_rsi(past))
    sources.append(analyze_macd(past))
    sources.append(analyze_bollinger(past))
    sources.append(analyze_multi_timeframe(past))
    sources.append(analyze_support_resistance(past))
    for volume_src in (analyze_volume(past), analyze_vwap(past)):
        if volume_src.weight > 0:
            sources.append(volume_src)

    if market in (Market.US_EQUITY, Market.IN_EQUITY) and macro_data:
        macro_visible = _macro_as_of(macro_data, visible[-1].ts)
        if macro_visible:
            sources.append(analyze_macro(macro_visible))

    if market in (Market.US_EQUITY, Market.IN_EQUITY):
        fundamentals_visible = _fundamentals_as_of(fundamentals_data, visible[-1].ts)
        if fundamentals_visible:
            sources.append(analyze_fundamentals(fundamentals_visible))

    news_visible = _news_as_of(news_data, visible[-1].ts)
    if news_visible:
        sentiment = analyze_sentiment(news_visible, asset=series.asset, now=visible[-1].ts)
        if sentiment.weight > 0:
            sources.append(sentiment)

    # Same two-part dampening as the live path: weights are scaled for the audit
    # trail, and the scalar is passed to synthesize because that is where it
    # actually affects confidence. The backtest must apply this identically to
    # the live scan or it is not measuring the same engine.
    #
    # No calendar scalar here: a backtest replays history, and applying today's
    # calendar to past bars would be lookahead. Historical event dampening would
    # need a point-in-time calendar, which is not something this cache holds.
    scalar = volatility_scalar(past)
    if scalar != 1.0:
        sources = [s.model_copy(update={"weight": round(s.weight * scalar, 4)}) for s in sources]
    sources.append(analyze_volatility(past))

    signal = synthesize(
        asset=series.asset,
        market=market,
        sources=sources,
        timeframe=timeframe,
        conviction_scalar=scalar,
    )
    # Invalidation follows the SYNTHESIZED direction, not the raw source's: a
    # zero-weight bullish source synthesizes to neutral, which must carry no level.
    invalidation = trend_invalidation(visible, signal.direction)
    return signal.model_copy(update={"invalidation_level": invalidation}), visible[-1].close


def _walk(
    series: PriceSeries,
    market: Market,
    timeframe: Timeframe,
    warmup: int,
    step: int,
    make_signal: Callable[[int], tuple[Signal, float]],
) -> BacktestReport:
    """Shared bar-by-bar walk: generate, filter neutrals, score forward."""
    candles = series.candles
    horizon = HORIZON_BARS[timeframe]

    scored: list[tuple[float, Outcome]] = []
    generated = 0
    directional = 0

    for t in range(warmup, len(candles) - 1, step):
        signal, entry = make_signal(t)
        generated += 1
        if signal.direction is Direction.NEUTRAL or entry == 0:
            continue
        directional += 1
        outcome = score_forward(
            direction=signal.direction,
            entry_price=entry,
            invalidation_level=signal.invalidation_level,
            future=candles[t + 1 :],
            horizon=horizon,
        )
        scored.append((signal.confidence, outcome))

    return BacktestReport(
        asset=series.asset,
        market=market,
        timeframe=timeframe,
        bars=len(candles),
        warmup=warmup,
        signals_generated=generated,
        directional=directional,
        summary=summarize_outcomes(scored),
    )


def run_backtest(
    series: PriceSeries,
    market: Market = Market.CRYPTO,
    timeframe: Timeframe = Timeframe.SWING,
    warmup: int = DEFAULT_WARMUP,
    step: int = 1,
    macro_data: dict[str, list[MacroObservation]] | None = None,
    news_data: list[NewsItem] | None = None,
    fundamentals_data: list[Fundamentals] | None = None,
) -> BacktestReport:
    """Backtest the full synthesis pipeline (everything `scan` runs)."""
    return _walk(
        series,
        market,
        timeframe,
        warmup,
        step,
        lambda t: signal_at(
            series,
            t,
            market=market,
            timeframe=timeframe,
            macro_data=macro_data,
            news_data=news_data,
            fundamentals_data=fundamentals_data,
        ),
    )


def run_analyzer_backtest(
    series: PriceSeries,
    analyzer: Callable[[PriceSeries], SignalSource],
    market: Market = Market.CRYPTO,
    timeframe: Timeframe = Timeframe.SWING,
    warmup: int = DEFAULT_WARMUP,
    step: int = 1,
) -> BacktestReport:
    """Backtest ONE analyzer in isolation, synthesized alone, so its hit rate
    and calibration can be compared against the blended pipeline's."""

    def make_signal(t: int) -> tuple[Signal, float]:
        visible = series.candles[: t + 1]
        past = PriceSeries(asset=series.asset, interval=series.interval, candles=visible)
        signal = synthesize(
            asset=series.asset,
            market=market,
            sources=[analyzer(past)],
            timeframe=timeframe,
        )
        invalidation = trend_invalidation(visible, signal.direction)
        return (
            signal.model_copy(update={"invalidation_level": invalidation}),
            visible[-1].close,
        )

    return _walk(series, market, timeframe, warmup, step, make_signal)


def run_per_analyzer_backtest(
    series: PriceSeries,
    market: Market = Market.CRYPTO,
    timeframe: Timeframe = Timeframe.SWING,
    warmup: int = DEFAULT_WARMUP,
    step: int = 1,
) -> dict[str, BacktestReport]:
    """Backtest every registered analyzer in isolation, plus the market's trend
    anchor and the full blended pipeline, keyed by name.

    This is the honest comparison table: does blending actually beat the
    single inputs on this asset, and which input is carrying the result?
    """
    trend_anchor = _TREND_ANALYZER.get(market, lambda s: analyze_trend(s, name="equity.trend"))
    reports: dict[str, BacktestReport] = {
        "trend": run_analyzer_backtest(
            series, trend_anchor, market=market, timeframe=timeframe, warmup=warmup, step=step
        )
    }
    for name, analyzer in ANALYZER_REGISTRY.items():
        reports[name] = run_analyzer_backtest(
            series, analyzer, market=market, timeframe=timeframe, warmup=warmup, step=step
        )
    reports["combined"] = run_backtest(
        series, market=market, timeframe=timeframe, warmup=warmup, step=step
    )
    return reports
