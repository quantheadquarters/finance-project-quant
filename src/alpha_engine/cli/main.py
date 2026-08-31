"""Command-line interface. This is what a developer-user runs first. It wires the
whole pipeline end to end:

    ingest -> cache -> analyze -> synthesize -> narrate -> record -> print

Run:
    python -m alpha_engine.cli.main scan BTC          # crypto, no key needed
    python -m alpha_engine.cli.main scan AAPL         # US equity, no key needed
    python -m alpha_engine.cli.main backtest BTC --days 365
    python -m alpha_engine.cli.main watch BTC AAPL NIFTY
    python -m alpha_engine.cli.main record-stats

Market is auto-detected (mapped crypto symbols -> crypto; Indian index symbols
-> F&O; `.NS` / `.BO` suffixes -> Indian equities; currency pairs like EURUSD
-> forex; everything else -> US equity) and can be forced with --market.
Equity scans blend the trend read with a macro-context tilt when FRED data is
available; with no FRED_API_KEY they degrade gracefully to trend-only. The
default crypto/US-equity paths never require a key; forex needs OANDA
credentials and live F&O chains need a broker key, both degrading to a clear
message without one.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from alpha_engine.analyzers.bollinger import analyze_bollinger
from alpha_engine.analyzers.crypto_trend import analyze_trend, trend_invalidation
from alpha_engine.analyzers.fno_oi import analyze_fno, oi_support_resistance
from alpha_engine.analyzers.forex_trend import analyze_forex_trend
from alpha_engine.analyzers.macd import analyze_macd
from alpha_engine.analyzers.macro_context import analyze_macro
from alpha_engine.analyzers.multi_timeframe import analyze_multi_timeframe
from alpha_engine.analyzers.rsi import analyze_rsi
from alpha_engine.analyzers.support_resistance import analyze_support_resistance
from alpha_engine.analyzers.volatility import analyze_volatility, volatility_scalar
from alpha_engine.analyzers.volume import analyze_volume
from alpha_engine.analyzers.vwap import analyze_vwap
from alpha_engine.cache.interface import Cache
from alpha_engine.cache.models import (
    Interval,
    MacroObservation,
    OptionsChain,
    PriceSeries,
)
from alpha_engine.ingestion import (
    binance,
    coingecko,
    coingecko_pro,
    fmp_prices,
    fred,
    oanda,
    yahoo,
)
from alpha_engine.ingestion.breeze import BreezeLiveClient
from alpha_engine.ingestion.indian_broker import BrokerNotConfiguredError
from alpha_engine.ingestion.indian_fno import load_indian_chain
from alpha_engine.narrative.narrator import write_thesis
from alpha_engine.quant.report import build_report, render_text
from alpha_engine.schema.signal import Market, Signal, SignalSource, Timeframe
from alpha_engine.synthesis.synthesize import synthesize
from alpha_engine.execution.executor import place_order
from alpha_engine.execution.orders import signal_to_order
from alpha_engine.validation.backtest import run_backtest, run_per_analyzer_backtest
from alpha_engine.validation.options_backtest import run_options_backtest
from alpha_engine.validation.outcomes import annotate_coverage, score_record, summarize_outcomes
from alpha_engine.validation.recorder import read_records, record_signal

# Indian index symbols that route to the F&O path. Extend as chains land.
_IN_FNO_SYMBOLS = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX"}
_IN_EQUITY_SUFFIXES = (".NS", ".BO")


def detect_market(asset: str, override: str | None = None) -> Market:
    """Mapped crypto symbols are crypto, known Indian indexes are F&O,
    currency pairs (EURUSD, EUR/USD) are forex, and everything else is a US
    equity ticker. Explicit --market always wins."""
    if override:
        return Market(override)
    asset = asset.upper()
    if coingecko.supports(asset):
        return Market.CRYPTO
    if asset in _IN_FNO_SYMBOLS:
        return Market.IN_FNO
    if asset.endswith(_IN_EQUITY_SUFFIXES):
        return Market.IN_EQUITY
    if oanda.supports(asset):
        return Market.FOREX
    return Market.US_EQUITY


def _load_series(
    asset: str, market: Market, days: int, no_refresh: bool, cache: Cache
) -> PriceSeries:
    """Shared cache-or-fetch path for commands that need a daily series. A fresh
    cache still gets refetched if it clearly covers less history than requested
    (e.g. `backtest --days 365` right after a 90-day scan) — silently backtesting
    a quarter when the user asked for a year would be quietly dishonest.

    `no_refresh=True` means NO NETWORK, including when nothing is cached at all.
    That used to be false: the miss branch read `series is None or (... and not
    no_refresh)`, so an empty cache fetched regardless of the flag. It is the
    guarantee `toolkit.py` calls non-negotiable — every tool passes
    `no_refresh=True` precisely so a public API cannot turn one request per
    ticker into one upstream fetch per ticker and get the host banned. An empty
    series is returned instead, and every caller already renders that as
    "no cached data for X; run `scan X` first".
    """
    series, stale = cache.get_price(asset, "1d")
    if series is None and no_refresh:
        return PriceSeries(asset=asset, interval=Interval.DAY, candles=[])

    too_short = series is not None and len(series.candles) < (days * 3) // 5
    if series is None or ((stale or too_short) and not no_refresh):
        if market is Market.CRYPTO:
            series = _fetch_crypto_daily(asset, days, cache)
        elif market is Market.FOREX:
            print(f"[ingest] fetching {asset} daily from OANDA...", file=sys.stderr)
            series = oanda.fetch_daily(asset, days=days, cache=cache)
        else:
            series = _fetch_equity_daily(asset, days, cache)
    else:
        print(f"[cache] using cached {asset} ({len(series.candles)} bars)", file=sys.stderr)
    return series


def _fetch_crypto_daily(asset: str, days: int, cache: Cache) -> PriceSeries:
    """Crypto fetch chain: CoinGecko Pro when a key exists, then keyless
    CoinGecko, then the keyless Binance fallback.

    Each step degrades loudly to the next so a CoinGecko 429 (rate limit)
    never kills a scan, and the last error propagates honestly if every
    source fails."""
    if coingecko_pro.has_key():
        try:
            print(f"[ingest] fetching {asset} daily from CoinGecko Pro...", file=sys.stderr)
            return coingecko_pro.fetch_daily(asset, days=days, cache=cache)
        except Exception as e:  # noqa: BLE001 - fall back to the keyless tier
            print(f"[ingest] CoinGecko Pro failed ({e}); trying keyless tier", file=sys.stderr)
    try:
        print(f"[ingest] fetching {asset} daily from CoinGecko...", file=sys.stderr)
        return coingecko.fetch_daily(asset, days=days, cache=cache)
    except Exception as e:  # noqa: BLE001 - fall back to Binance
        print(f"[ingest] CoinGecko failed ({e}); falling back to Binance", file=sys.stderr)
    return binance.fetch_daily(asset, days=days, cache=cache)


def _fetch_equity_daily(asset: str, days: int, cache: Cache) -> PriceSeries:
    """Equity fetch chain: keyless Yahoo first, then FMP when a key exists.

    Mirrors `_fetch_crypto_daily`. Yahoo stays the primary because it needs no
    key and the default path must stay keyless; FMP is a second tier for
    operators who have already set `FMP_API_KEY` for fundamentals.

    Yahoo throttles by IP with HTTP 429 and there is no keyless alternative left
    to fall back to — every candidate now sits behind a bot challenge — so this
    tier is the difference between "the scan degrades today" and "the scan
    degrades today and every retry until the throttle lifts".
    """
    try:
        print(f"[ingest] fetching {asset} daily from Yahoo Finance...", file=sys.stderr)
        return yahoo.fetch_daily(asset, days=days, cache=cache)
    except Exception as e:  # noqa: BLE001 - fall through to the keyed tier
        if not fmp_prices.has_key():
            # Nothing to fall back to; the caller sees the real Yahoo error
            # rather than a misleading one about a key it never configured.
            raise
        print(
            f"[ingest] Yahoo failed for {asset} ({e}); trying FMP",
            file=sys.stderr,
        )
    return fmp_prices.fetch_daily(asset, days=days, cache=cache)


def _load_macro(cache: Cache, no_refresh: bool) -> dict[str, list[MacroObservation]]:
    """Best-effort macro data: serve from cache, refresh stale series only when a
    FRED key exists, and never crash the scan over macro. Empty dict = no data."""
    data: dict[str, list[MacroObservation]] = {}
    have_key = bool(os.environ.get("FRED_API_KEY"))
    for series_id in fred.MACRO_SERIES:
        obs, stale = cache.get_macro(series_id)
        if (not obs or (stale and not no_refresh)) and have_key:
            try:
                print(f"[ingest] fetching {series_id} from FRED...", file=sys.stderr)
                obs = fred.fetch_series(series_id, cache=cache)
            except Exception as e:  # noqa: BLE001 - macro is optional context
                print(f"[macro] {series_id} fetch failed: {e}", file=sys.stderr)
        if obs:
            data[series_id] = obs
    if not data and not have_key:
        print(
            "[macro] FRED_API_KEY not set; scanning without macro context "
            "(free key: https://fred.stlouisfed.org)",
            file=sys.stderr,
        )
    return data


# --- Phase 11 context loaders -----------------------------------------------
#
# These are deliberately READ-ONLY. `cache/interface.py` states the rule the
# whole architecture rests on: "analyzers read from HERE, never from the
# network. An ingestion service keeps the store fresh; consumers just read."
#
# Price and macro predate that rule and still refresh inline, because a scan is
# meaningless without a price series. Context is different: fetching four RSS
# feeds, two Binance endpoints and a CoinGecko call on every `scan` would turn a
# sub-second command into a multi-second one, hammer free APIs, and make the
# test suite hit the network. Refresh belongs to `ingest` and the orchestrator's
# freshness pass, which is exactly what those exist for.
#
# Empty here means "nothing cached yet", and the analyzer degrades honestly.


def _load_news(cache: Cache) -> list[Any]:
    """Cached headlines. Populate with `ingest news`. Phase 11a."""
    items, _ = cache.get_news()
    return items


def _load_onchain(cache: Cache) -> list[Any]:
    """Cached crypto positioning data. Populate with `ingest onchain`. Phase 11b."""
    items, _ = cache.get_onchain()
    return items


def _load_fundamentals(cache: Cache, asset: str) -> list[Any]:
    """Cached fundamentals. Populate with `ingest fundamentals`. Phase 11c."""
    items, _ = cache.get_fundamentals(asset)
    return items


def _load_events(cache: Cache) -> list[Any]:
    """Cached macro calendar. Curated by hand or by a scheduled job. Phase 11d."""
    events, _ = cache.get_events()
    return events


def _scan_fno(asset: str, cache: Cache, args: argparse.Namespace, use_llm: bool = False) -> int:
    """The Indian F&O path reads an options chain, not a price series, so it has
    its own flow. `scan` itself never fetches a chain: it reads whatever the
    cache holds (put there by `fetch-chain` with broker credentials, or by
    `scan-chain` from a JSON fixture). Missing data degrades to a clear
    message, never a crash."""
    chain, stale = cache.get_chain(asset)
    if chain is None:
        print(
            f"[error] no options chain cached for {asset}. Fetch one live with "
            f"`fetch-chain {asset} --expiry YYYY-MM-DD --broker breeze|angelone|dhan` "
            f"(needs broker credentials in .env), or run the analytics offline by "
            f"dropping a normalized OptionsChain JSON at data/cache/chain/{asset}.json "
            f"and using `scan-chain` (see cache/models.py for the shape).",
            file=sys.stderr,
        )
        return 1
    return _scan_fno_chain(asset, chain, stale, cache, args, use_llm=use_llm)


def _scan_fno_chain(
    asset: str,
    chain: OptionsChain,
    stale: bool,
    cache: Cache,
    args: argparse.Namespace,
    use_llm: bool = False,
) -> int:
    """Run the deterministic F&O pipeline on a normalized chain."""
    if stale:
        print(
            f"[cache] warning: {asset} chain is stale (fetched {chain.fetched_at}); "
            f"OI reads age quickly",
            file=sys.stderr,
        )

    signal = _build_fno_signal(asset, chain, use_llm=use_llm)

    if not args.no_record:
        record = record_signal(signal, entry_price=chain.spot)
        print(f"[record] appended {record.record_id} to data/signals/", file=sys.stderr)

    print(signal.model_dump_json(indent=2))
    return 0


def _fetch_fno_chain(
    asset: str,
    expiry_date: str,
    cache: Cache,
    args: argparse.Namespace,
) -> int:
    """Fetch a live Indian options chain and immediately run the deterministic
    F&O analyzer on it.

    Supports Breeze and Angel One. The broker is selected via --broker flag
    (default: breeze). The fetch is deliberately gated behind env credentials
    so the default repo still stays keyless.
    """
    broker = getattr(args, "broker", "breeze")

    try:
        if broker == "angelone":
            from alpha_engine.ingestion.angelone import AngelOneLiveClient

            client = AngelOneLiveClient.from_env()
        elif broker == "dhan":
            from alpha_engine.ingestion.dhan import DhanLiveClient

            client = DhanLiveClient.from_env()
        else:
            client = BreezeLiveClient.from_env()
        chain = client.fetch_chain(asset, expiry_date)
    except BrokerNotConfiguredError as e:
        print(f"[error] {e}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001 - surface live broker issues clearly
        print(f"[error] {broker} fetch failed: {e}", file=sys.stderr)
        return 1

    cache.put_chain(chain)
    print(
        f"[cache] saved {asset} options chain for {expiry_date} to data/cache/chain/",
        file=sys.stderr,
    )
    return _scan_fno_chain(asset, chain, False, cache, args)


def _build_fno_signal(asset: str, chain: OptionsChain, use_llm: bool = False) -> Signal:
    """Build the deterministic F&O signal for one chain."""
    source = analyze_fno(chain)
    signal = synthesize(
        asset=asset,
        market=Market.IN_FNO,
        sources=[source],
        timeframe=Timeframe.SWING,
    )
    invalidation = oi_support_resistance(chain, signal.direction)
    signal = signal.model_copy(update={"invalidation_level": invalidation})
    signal = write_thesis(signal, use_llm=use_llm)
    return signal


def _load_chain_file(path: str | Path, underlying: str | None = None) -> OptionsChain:
    """Load a normalized options-chain fixture from disk."""
    return load_indian_chain(path, underlying=underlying)


def cmd_scan_chain(args: argparse.Namespace) -> int:
    """Analyze a normalized options-chain fixture from disk.

    This is the offline Phase 3 entry point: no broker credentials, no network,
    just the deterministic PCR / max-pain / OI-shift math against a fixture.
    The loaded chain is also written to the local cache so `scan NIFTY` can
    reuse the same normalized payload afterward.
    """
    cache = Cache()
    try:
        chain = _load_chain_file(args.chain_file, underlying=getattr(args, "underlying", None))
    except Exception as e:  # noqa: BLE001 - fixture loading should fail loudly
        print(f"[error] failed to load chain fixture: {e}", file=sys.stderr)
        return 1

    cache.put_chain(chain)
    return _scan_fno_chain(chain.underlying, chain, False, cache, args)


def cmd_fetch_chain(args: argparse.Namespace) -> int:
    """Fetch a live Indian options chain from Breeze and analyze it."""
    cache = Cache()
    asset = args.asset.upper()
    return _fetch_fno_chain(asset, args.expiry, cache, args)


def cmd_scan(args: argparse.Namespace) -> int:
    cache = Cache()
    asset = args.asset.upper()
    market = detect_market(asset, args.market)
    use_llm = getattr(args, "llm", False)

    if market is Market.IN_FNO:
        return _scan_fno(asset, cache, args, use_llm=use_llm)

    try:
        series = _load_series(asset, market, args.days, args.no_refresh, cache)
    except Exception as e:  # noqa: BLE001 - surface any fetch issue clearly
        print(f"[error] fetch failed: {e}", file=sys.stderr)
        return 1

    signal = _build_price_signal(asset, market, series, cache, args.no_refresh, use_llm=use_llm)

    if not args.no_record:
        entry = series.candles[-1].close if series.candles else None
        record = record_signal(signal, entry_price=entry)
        print(f"[record] appended {record.record_id} to data/signals/", file=sys.stderr)

    print(signal.model_dump_json(indent=2))
    return 0


def _build_price_signal(
    asset: str,
    market: Market,
    series: PriceSeries,
    cache: Cache,
    no_refresh: bool,
    use_llm: bool = False,
) -> Signal:
    """Build the deterministic price-series signal for crypto or equities.

    Uses multiple analyzers for a richer synthesis:
    - Trend (dual MA) — core directional read (market-specific)
    - RSI, MACD — momentum confirmation
    - Bollinger Bands — volatility/position context
    - Volume (OBV), VWAP — participation reads (skipped when volume is absent)
    - Support/resistance, multi-horizon alignment — structure reads
    - Macro context (equities only) — when FRED/RBI data available, region-aware
    - News sentiment — deterministic lexicon over cached headlines (Phase 11a)
    - Crypto positioning — funding, OI, flows, dominance (Phase 11b)
    - Fundamentals — accruals, leverage, growth (Phase 11c, key-gated)
    - Forex carry — rate differential and dollar cycle (Phase 11e)
    - Volatility regime — contextual; extreme tape dampens every other weight
    - Macro calendar — dampens confidence before known events (Phase 11d)
    """
    from alpha_engine.analyzers.crypto_onchain import analyze_onchain
    from alpha_engine.analyzers.forex_carry import analyze_forex_carry
    from alpha_engine.analyzers.fundamentals import analyze_fundamentals
    from alpha_engine.analyzers.indian_equity import analyze_indian_equity
    from alpha_engine.analyzers.macro_calendar import calendar_note, calendar_scalar
    from alpha_engine.analyzers.sentiment import analyze_sentiment

    sources: list[SignalSource] = []

    # Core directional read is market-specific; everything after is shared.
    if market is Market.CRYPTO:
        sources.append(analyze_trend(series))
    elif market is Market.IN_EQUITY:
        sources.append(analyze_indian_equity(series))
    elif market is Market.FOREX:
        sources.append(analyze_forex_trend(series))
    else:
        sources.append(analyze_trend(series, name="equity.trend"))

    sources.append(analyze_rsi(series))
    sources.append(analyze_macd(series))
    sources.append(analyze_bollinger(series))
    sources.append(analyze_multi_timeframe(series))
    sources.append(analyze_support_resistance(series))
    for volume_src in (analyze_volume(series), analyze_vwap(series)):
        if volume_src.weight > 0:
            sources.append(volume_src)

    if market in (Market.IN_EQUITY, Market.US_EQUITY):
        macro_data = _load_macro(cache, no_refresh)
        if macro_data:
            region = "in" if market is Market.IN_EQUITY else "us"
            sources.append(analyze_macro(macro_data, region=region))

        fundamentals = _load_fundamentals(cache, asset)
        if fundamentals:
            sources.append(analyze_fundamentals(fundamentals))

    if market is Market.CRYPTO:
        onchain = _load_onchain(cache)
        if onchain:
            sources.append(analyze_onchain(onchain, asset=asset))

    if market is Market.FOREX:
        macro_data = _load_macro(cache, no_refresh)
        carry = analyze_forex_carry(series, macro=macro_data)
        if carry.weight > 0:
            sources.append(carry)

    # News applies to every market; the analyzer filters by asset tag itself.
    news = _load_news(cache)
    if news:
        sentiment = analyze_sentiment(news, asset=asset)
        if sentiment.weight > 0:
            sources.append(sentiment)

    # Two regime layers, both purely defensive: an extreme tape and a looming
    # policy decision each mean the model knows less than usual.
    #
    # They scale source weights (so the audit trail shows the inputs were
    # discounted) AND are passed to synthesize as a conviction scalar, which is
    # what actually lowers confidence. Weight scaling alone does nothing —
    # every term in the confidence formula is a ratio, so a constant factor
    # cancels out. See synthesize()'s docstring.
    vol_scalar = volatility_scalar(series)
    if vol_scalar != 1.0:
        sources = [
            s.model_copy(update={"weight": round(s.weight * vol_scalar, 4)}) for s in sources
        ]
    sources.append(analyze_volatility(series))

    events = _load_events(cache)
    cal_scalar = calendar_scalar(events, market.value)
    if cal_scalar < 1.0:
        sources = [
            s.model_copy(update={"weight": round(s.weight * cal_scalar, 4)}) for s in sources
        ]
        print(
            f"[calendar] dampening conviction x{cal_scalar:.2f} — "
            f"{calendar_note(events, market.value)}",
            file=sys.stderr,
        )

    signal = synthesize(
        asset=asset,
        market=market,
        sources=sources,
        timeframe=Timeframe.SWING,
        conviction_scalar=vol_scalar * cal_scalar,
    )
    invalidation = trend_invalidation(series.candles, signal.direction)
    signal = signal.model_copy(update={"invalidation_level": invalidation})
    signal = write_thesis(signal, use_llm=use_llm)
    return signal


def cmd_backtest(args: argparse.Namespace) -> int:
    cache = Cache()
    asset = args.asset.upper()
    market = detect_market(asset, args.market)

    if market is Market.IN_FNO:
        print(
            "[error] F&O backtesting needs a history of options chains, which no "
            "free source provides yet. Price-series backtests cover crypto and "
            "US equities.",
            file=sys.stderr,
        )
        return 1

    try:
        series = _load_series(asset, market, args.days, args.no_refresh, cache)
    except Exception as e:  # noqa: BLE001
        print(f"[error] fetch failed: {e}", file=sys.stderr)
        return 1

    if getattr(args, "options", False):
        # Joint underlying + ATM-option backtest. Option leg is Black-Scholes
        # model-priced (no free tick-level option history exists) — see
        # validation/options_backtest.py for the honesty boundaries.
        opt_report = run_options_backtest(series, market=market, step=args.step)
        print(opt_report.model_dump_json(indent=2))
        return 0

    if getattr(args, "per_analyzer", False):
        reports = run_per_analyzer_backtest(series, market=market, step=args.step)
        print(
            json.dumps({name: r.model_dump(mode="json") for name, r in reports.items()}, indent=2)
        )
        return 0

    # Equity backtests replay macro context point-in-time (observations dated
    # after the simulated bar stay invisible — see backtest.py).
    macro_data = None
    if market in (Market.US_EQUITY, Market.IN_EQUITY):
        macro_data = _load_macro(cache, args.no_refresh) or None

    report = run_backtest(series, market=market, step=args.step, macro_data=macro_data)
    print(report.model_dump_json(indent=2))
    return 0


def cmd_trade(args: argparse.Namespace) -> int:
    """Scan an asset and place ONE order from the resulting signal.

    Paper by default: nothing reaches a broker unless LIVE_TRADING=1. Non-
    actionable signals (neutral / low confidence) place no order at all.
    """
    cache = Cache()
    asset = args.asset.upper()
    market = detect_market(asset, args.market)

    if market is Market.IN_FNO:
        print(
            "[error] pass the UNDERLYING (e.g. NIFTY, RELIANCE) with --option, "
            "not an F&O contract symbol.",
            file=sys.stderr,
        )
        return 1

    try:
        series = _load_series(asset, market, args.days, args.no_refresh, cache)
    except Exception as e:  # noqa: BLE001 - surface any fetch issue clearly
        print(f"[error] fetch failed: {e}", file=sys.stderr)
        return 1

    signal = _build_price_signal(asset, market, series, cache, args.no_refresh)
    spot = series.candles[-1].close if series.candles else 0.0

    order = signal_to_order(
        signal,
        spot=spot,
        quantity=args.qty,
        as_option=args.option,
        strike_step=args.strike_step,
        expiry=args.expiry,
        product=args.product,
    )
    if order is None:
        print(
            f"[trade] signal not actionable ({signal.direction.value}, "
            f"conf {signal.confidence:.2f}) — no order placed.",
            file=sys.stderr,
        )
        print(signal.model_dump_json(indent=2))
        return 0

    if args.option and not args.expiry:
        print(
            "[trade] WARNING: option order has no --expiry; a live broker will "
            "reject it. Fine for a paper check.",
            file=sys.stderr,
        )

    result = place_order(order, broker=args.broker, est_price=spot)
    banner = "LIVE ORDER SENT" if result.status == "live" else f"{result.status.upper()}"
    leg = order.instrument.value
    if order.instrument.value == "option":
        leg = f"{order.strike:g} {order.right.value}"  # type: ignore[union-attr]
    print(
        f"[trade] {banner}: {order.side.value} {order.quantity} {asset} {leg}  "
        f"(broker={result.broker})",
        file=sys.stderr,
    )
    print(result.model_dump_json(indent=2))
    return 0


def cmd_webhook(args: argparse.Namespace) -> int:
    """Run the inbound trade webhook. Paper unless LIVE_TRADING=1."""
    from alpha_engine.execution.webhook import serve

    try:
        serve(host=args.host, port=args.port)
    except RuntimeError as e:
        print(f"[error] {e}", file=sys.stderr)
        return 1
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    """Full quant metrics report: regime, scores, volatility forecast,
    fair-value distances, indicator values and a templated verdict. Same
    fetch path as scan/backtest; all numbers deterministic."""
    cache = Cache()
    asset = args.asset.upper()
    market = detect_market(asset, args.market)

    if market is Market.IN_FNO:
        print(
            "[error] the quant report needs a price series; F&O indexes have "
            "options chains, not candles. Try the underlying via Yahoo "
            "(e.g. ^NSEI with --market us_equity) or use scan-chain.",
            file=sys.stderr,
        )
        return 1

    try:
        series = _load_series(asset, market, args.days, args.no_refresh, cache)
    except Exception as e:  # noqa: BLE001 - surface any fetch issue clearly
        print(f"[error] fetch failed: {e}", file=sys.stderr)
        return 1

    try:
        report = build_report(series, market=market.value)
    except ValueError as e:
        print(f"[error] {e}", file=sys.stderr)
        return 1

    if args.json:
        print(report.model_dump_json(indent=2))
    else:
        print(render_text(report))
    return 0


def _format_table(rows: list[dict[str, str]]) -> str:
    headers = ["asset", "market", "direction", "confidence", "status", "note"]
    widths = {h: len(h) for h in headers}
    for row in rows:
        for h in headers:
            widths[h] = max(widths[h], len(row.get(h, "")))

    def fmt(values: dict[str, str]) -> str:
        return "  ".join(values.get(h, "").ljust(widths[h]) for h in headers)

    lines = [fmt({h: h for h in headers})]
    lines.append("  ".join("-" * widths[h] for h in headers))
    for row in rows:
        lines.append(fmt(row))
    return "\n".join(lines)


def cmd_watch(args: argparse.Namespace) -> int:
    """Scan a batch of assets and print a compact table.

    This is a CLI convenience layer on top of the same deterministic analyzers
    used by `scan`; it is intentionally read-friendly, not a trading surface.
    """
    cache = Cache()
    rows: list[dict[str, str]] = []
    use_llm = getattr(args, "llm", False)

    for raw_asset in args.assets:
        asset = raw_asset.upper()
        market = detect_market(asset, args.market)
        try:
            if market is Market.IN_FNO:
                chain, stale = cache.get_chain(asset)
                if chain is None:
                    raise FileNotFoundError(
                        f"no cached chain for {asset}; run scan-chain first or drop a JSON file"
                    )
                signal = _build_fno_signal(asset, chain, use_llm=use_llm)
                status = "stale" if stale else "ok"
                if args.record:
                    record = record_signal(signal, entry_price=chain.spot)
                    print(
                        f"[record] appended {record.record_id} for {asset} to data/signals/",
                        file=sys.stderr,
                    )
            else:
                series = _load_series(asset, market, args.days, args.no_refresh, cache)
                signal = _build_price_signal(
                    asset, market, series, cache, args.no_refresh, use_llm=use_llm
                )
                status = "stale" if cache.get_price(asset, "1d")[1] else "ok"
                if args.record:
                    entry = series.candles[-1].close if series.candles else None
                    record = record_signal(signal, entry_price=entry)
                    print(
                        f"[record] appended {record.record_id} for {asset} to data/signals/",
                        file=sys.stderr,
                    )
        except Exception as e:  # noqa: BLE001 - batch view should keep going
            rows.append(
                {
                    "asset": asset,
                    "market": market.value,
                    "direction": "error",
                    "confidence": "-",
                    "status": str(e),
                    "note": "",
                }
            )
            continue

        note = signal.signal_sources[0].detail[:80] if signal.signal_sources else ""

        rows.append(
            {
                "asset": asset,
                "market": market.value,
                "direction": signal.direction.value,
                "confidence": f"{signal.confidence:.2f}",
                "status": status,
                "note": note,
            }
        )

    sort = getattr(args, "sort", None)
    if sort:
        if sort == "confidence":
            rows.sort(
                key=lambda row: float(row["confidence"]) if row["confidence"] != "-" else -1.0,
                reverse=True,
            )
        else:
            rows.sort(key=lambda row: row[sort])

    print(_format_table(rows))
    return 0


def cmd_record_stats(args: argparse.Namespace) -> int:
    """Score every recorded live signal against cached prices. Reads only the
    local cache — run a fresh `scan` first if you want newer price data."""
    records = read_records()
    if not records:
        print("[record-stats] no recorded signals yet; run `scan` first.", file=sys.stderr)
        return 0

    cache = Cache()
    scored = []
    missing_assets: set[str] = set()
    for record in records:
        series, _stale = cache.get_price(record.signal.asset, "1d")
        if series is None:
            missing_assets.add(record.signal.asset)
            continue  # asset no longer cached; disclosed in the payload below
        scored.append((record.signal.confidence, score_record(record, series)))

    skipped = len(records) - len(scored)

    payload = annotate_coverage(
        summarize_outcomes(scored).model_dump(),
        total=len(records),
        scored=len(scored),
        missing_assets=missing_assets,
    )

    if skipped:
        print(
            f"[record-stats] {skipped} of {len(records)} record(s) skipped, "
            f"no cached prices for: {', '.join(sorted(missing_assets))}",
            file=sys.stderr,
        )

    print(json.dumps(payload, indent=2))
    return 0


def cmd_scan_all(args: argparse.Namespace) -> int:
    """Scan all configured assets across all markets. The batch entry point."""
    from alpha_engine.orchestrator import (
        load_config,
        run_batch,
    )

    config = load_config(
        config_path=getattr(args, "config", None),
        assets=getattr(args, "assets", None),
        days=args.days,
        record=not args.no_record,
        use_llm=getattr(args, "llm", False),
    )

    print(f"[scan-all] scanning {len(config.targets)} assets...", file=sys.stderr)
    report = run_batch(config)

    summary = report.summary()
    print(
        f"\n[scan-all] done: {summary['ok']}/{summary['total']} ok, {summary['errors']} errors",
        file=sys.stderr,
    )
    print(json.dumps(summary, indent=2))
    return 0 if summary["errors"] == 0 else 1


def cmd_batch(args: argparse.Namespace) -> int:
    """Run a scheduled batch scan with report output. Cron-friendly."""

    from alpha_engine.orchestrator import (
        load_config,
        run_scheduled,
    )

    config = load_config(
        config_path=getattr(args, "config", None),
        assets=getattr(args, "assets", None),
        days=args.days,
        record=not args.no_record,
        use_llm=getattr(args, "llm", False),
    )

    output = getattr(args, "output", None)
    report = run_scheduled(config, output_path=output)

    if output:
        print(f"[batch] report written to {output}", file=sys.stderr)

    summary = report.summary()
    print(
        f"[batch] done: {summary['ok']}/{summary['total']} ok, {summary['errors']} errors",
        file=sys.stderr,
    )
    return 0 if summary["errors"] == 0 else 1


def cmd_ingest(args: argparse.Namespace) -> int:
    """Refresh the Phase 11 context caches (news, on-chain, fundamentals).

    The scan path reads these cache-only by design, so this is what fills them.
    Run it on a schedule (or let `orchestrate` do it) rather than on every scan:
    hammering four RSS feeds and three APIs per signal is how you get rate
    limited and how a one-second command becomes a ten-second one.
    """
    from alpha_engine.orchestrator.engine import refresh_context, stale_kinds

    cache = Cache()
    assets = tuple(a.upper() for a in (args.assets or ["BTC", "ETH", "AAPL"]))

    kinds = set(args.kind) if args.kind else None
    if kinds is None and not args.force:
        kinds = stale_kinds(cache, assets)
        if not kinds:
            print("[ingest] everything is fresh; nothing to do", file=sys.stderr)
            return 0
        print(f"[ingest] stale: {', '.join(sorted(kinds))}", file=sys.stderr)

    report = refresh_context(cache, assets, kinds=kinds, force=args.force)

    if args.json:
        print(json.dumps(report.summary(), indent=2))
    else:
        if report.refreshed:
            print(f"refreshed: {', '.join(sorted(report.refreshed))}")
        if report.skipped_fresh:
            print(f"already fresh: {', '.join(sorted(report.skipped_fresh))}")
        for kind, err in report.failed.items():
            print(f"FAILED {kind}: {err}", file=sys.stderr)

    # A feed that answered but returned nothing is the failure mode this project
    # exists to catch, and it is invisible in the exit code below because every
    # adapter degrades to empty instead of raising. Report it always; escalate
    # it to an exit code only under --strict.
    empty_feeds = [
        name
        for name, count in sorted(report.item_counts.items())
        if count == 0 and "." in name and report.item_counts.get(name.split(".")[0], 0) > 0
    ]
    for name in empty_feeds:
        print(
            f"[ingest] WARNING {name}: 0 items (another feed covered this kind)",
            file=sys.stderr,
        )

    # A whole KIND at zero is different: nothing covered it, so the analyzers
    # that read it will silently score without their input from now on.
    dead_kinds = [
        kind
        for kind in sorted(report.refreshed)
        if report.item_counts.get(kind, 0) == 0 and kind not in report.failed
    ]
    for kind in dead_kinds:
        print(f"[ingest] EMPTY {kind}: every feed returned 0 items", file=sys.stderr)

    if args.strict and (dead_kinds or report.failed):
        print(
            "[ingest] --strict: exiting non-zero so a scheduled run turns red "
            "instead of succeeding with no data",
            file=sys.stderr,
        )
        return 1
    return 1 if report.failed else 0


def cmd_health(args: argparse.Namespace) -> int:
    """Report which data sources are working and which have gone quiet.

    This is the answer to the way scrapers actually fail. They rarely crash —
    they return nothing, forever, and every signal afterwards is quietly weaker.
    Exits non-zero when a source is degraded so a cron job can surface it.
    """
    from alpha_engine.health import load_health
    from alpha_engine.ingestion.rss import FEEDS, feed_status

    report = load_health()

    # Feeds that are switched off by configuration are not failures, and must
    # not be reported as if they were — but they still need to be visible, or
    # "why is there no SEC data" has no answer anywhere.
    disabled = {}
    for name in FEEDS:
        enabled, reason = feed_status(name)
        if not enabled:
            disabled[f"news.{name}"] = reason

    if not report.sources and not disabled:
        print("No health data yet. Run `ingest` (or the daily job) first.")
        return 0

    if args.json:
        payload = report.summary()
        payload["disabled"] = disabled
        print(json.dumps(payload, indent=2))
    else:
        labels = {"ok": "ok", "degraded": "WARN", "broken": "FAIL", "unknown": "?"}
        rows = [
            (n, labels.get(e.status(), e.status()), e.explain()) for n, e in report.sources.items()
        ]
        rows += [(n, "off", r) for n, r in disabled.items()]
        width = max((len(n) for n, _, _ in rows), default=10) + 2

        print(f"\n{'source':<{width}} {'status':<6} detail")
        print("-" * (width + 60))
        for name, state, detail in sorted(rows):
            print(f"{name:<{width}} {state:<6} {detail}")
        print()

    degraded = report.degraded()
    if degraded:
        # stdout, not stderr: this is the answer to the question the user asked,
        # and mixing the two streams in a terminal prints the warnings *above*
        # the table they refer to. Cron captures both anyway.
        print(f"{len(degraded)} source(s) need attention:")
        for entry in degraded:
            print(f"  {entry.source}: {entry.explain()}")
        print(
            "\nA degraded source is not fatal — the engine keeps running with a\n"
            "narrower read. But signals produced now are weaker than they look.\n"
        )
        return 1 if args.strict else 0

    return 0


def cmd_orchestrate(args: argparse.Namespace) -> int:
    """Run the event-driven orchestrator: build triggers, order them by
    priority, and execute them against one shared context.

    Without --news this is a scheduled sweep. With it, recent tagged headlines
    become targeted per-asset re-scans that run *before* the routine sweep.
    """
    from alpha_engine.orchestrator import load_config
    from alpha_engine.orchestrator.engine import (
        TriggerQueue,
        run_triggers,
        scheduled_trigger,
        triggers_from_news,
        user_trigger,
    )

    cache = Cache()
    config = load_config(
        config_path=args.config,
        assets=args.assets,
        days=args.days,
        record=not args.no_record,
    )
    assets = tuple(t.asset for t in config.targets)
    if not assets:
        print("[error] no assets configured", file=sys.stderr)
        return 1

    queue = TriggerQueue()

    if args.news:
        news_triggers = triggers_from_news(cache, assets, max_age_hours=args.news_age)
        for trigger in news_triggers:
            queue.push(trigger)
        print(f"[orchestrator] {len(news_triggers)} news-driven triggers", file=sys.stderr)

    if args.assets and not args.news:
        queue.push(user_trigger(assets))
    elif not args.news_only:
        queue.push(scheduled_trigger(assets))

    if not queue:
        print("[orchestrator] nothing to do", file=sys.stderr)
        return 0

    report = run_triggers(queue, config, cache=cache, refresh=not args.no_refresh_context)

    if args.json:
        print(json.dumps(report.summary(), indent=2))
    else:
        print(f"\nexecuted {len(report.executed)} triggers")
        for row in report.executed:
            direction = row.get("direction") or "-"
            print(f"  {row['trigger']:<12} {row['asset']:<10} {row['status']:<8} {direction}")
        print(f"\ncontext: {report.context_stats}")

    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(json.dumps(report.summary(), indent=2))
        print(f"[orchestrator] report written to {args.output}", file=sys.stderr)

    return 0


def cmd_dashboard(args: argparse.Namespace) -> int:
    """Launch the web app: dashboard, AI terminal, and the HTTP/MCP API."""
    import sys as _sys

    host = getattr(args, "host", "127.0.0.1")
    port = getattr(args, "port", 8000)

    forwarded = [f"--host={host}", f"--port={port}"]
    if getattr(args, "allow_writes", False):
        forwarded.append("--allow-writes")
    if getattr(args, "rate_limit", None) is not None:
        forwarded.append(f"--rate-limit={args.rate_limit}")
    if getattr(args, "cors", None):
        forwarded.append(f"--cors={args.cors}")
    if getattr(args, "allow_public", False):
        forwarded.append("--allow-public")
    if getattr(args, "log_requests", False):
        forwarded.append("--log-requests")

    try:
        from alpha_engine.web.server import main as web_main
    except ImportError as e:  # pragma: no cover - a damaged install, not a flow
        # The web app now ships inside the package, so this can only mean a
        # broken installation. It used to mean "you pip-installed and `web/`
        # was not included", which was a real limitation and the reason for
        # the move.
        print(f"[error] could not import the web app: {e}", file=_sys.stderr)
        return 1

    return web_main(forwarded)


def cmd_strategies(args: argparse.Namespace) -> int:
    """List the strategies available to `strategy-backtest`."""
    from alpha_engine.strategy.loader import list_strategies

    catalogue = list_strategies()
    strategies = catalogue["strategies"]

    if not strategies:
        print("No strategies found.")
    for entry in strategies:
        print(f"\n  {entry['key']}  —  {entry['name']}")
        print(f"    {entry['description']}")
        if entry["params"]:
            params = ", ".join(f"{k}={v}" for k, v in entry["params"].items())
            print(f"    params: {params}")

    print(f"\nStrategy folder: {catalogue['strategy_dir']}")
    print("Drop a BaseStrategy subclass in there and it appears here automatically.")

    if catalogue["errors"]:
        print("\nFiles that failed to load:", file=sys.stderr)
        for name, error in catalogue["errors"].items():
            print(f"  {name}: {error}", file=sys.stderr)
    return 0


def cmd_strategy_backtest(args: argparse.Namespace) -> int:
    """Trade-level backtest of one strategy: equity curve, trades, Sharpe.

    Distinct from `backtest`, which measures whether the ENGINE's own signals
    were directionally right. This measures what a position book following your
    rule would have done — including transaction costs and drawdown.
    """
    from alpha_engine.strategy.engine import run_strategy_backtest
    from alpha_engine.strategy.loader import load_strategy

    from alpha_engine.strategy.csv_data import CsvFormatError, load_series

    interval = Interval(args.interval)
    cache = Cache()

    # --csv is the path for data no API serves — Indian option history above all,
    # which is what the original nifty_backtester existed to work on.
    if args.csv:
        try:
            series = load_series(args.csv, asset=args.asset, interval=interval)
        except CsvFormatError as e:
            print(f"[error] {e}", file=sys.stderr)
            return 1
        asset, market = series.asset, detect_market(series.asset, args.market)
    else:
        asset = args.asset.upper()
        market = detect_market(asset, args.market)
        series = _load_series(asset, market, args.days, args.no_refresh, cache)
        if not series.candles:
            print(f"[error] no price data for {asset}", file=sys.stderr)
            return 1

    option_series = None
    if args.option_csv:
        try:
            option_series = load_series(args.option_csv, interval=interval)
        except CsvFormatError as e:
            print(f"[error] option CSV: {e}", file=sys.stderr)
            return 1
    elif args.option:
        option_asset = args.option.upper()
        option_series = _load_series(
            option_asset,
            detect_market(option_asset, args.market),
            args.days,
            args.no_refresh,
            cache,
        )
        if not option_series.candles:
            print(f"[error] no price data for option {option_asset}", file=sys.stderr)
            return 1

    params: dict[str, float | int | str] = {}
    for pair in args.param or []:
        if "=" not in pair:
            print(f"[error] --param expects name=value, got '{pair}'", file=sys.stderr)
            return 2
        key, raw = pair.split("=", 1)
        try:
            params[key.strip()] = int(raw)
        except ValueError:
            try:
                params[key.strip()] = float(raw)
            except ValueError:
                params[key.strip()] = raw.strip()

    try:
        strategy = load_strategy(args.strategy, **params)
        report = run_strategy_backtest(
            strategy,
            series,
            option_series,
            trade_on=args.trade_on,
            require_option_confirmation=not args.no_confirmation,
            capital=args.capital,
            txn_cost_bps=args.cost_bps,
        )
    except (KeyError, ValueError) as e:
        print(f"[error] {str(e).strip(chr(39))}", file=sys.stderr)
        return 1

    if args.json:
        print(report.model_dump_json(indent=2))
        return 0

    m = report.metrics
    print(f"\n{report.strategy} on {report.asset}  ({report.bars} bars, {report.interval.value})")
    if report.params:
        print("  params: " + ", ".join(f"{k}={v}" for k, v in report.params.items()))
    print(f"  P&L on: {report.trade_on}", end="")
    if report.option_confirmation:
        print(f"   ·   option-confirmed: {report.signals_confirmed}/{report.signals_raw} signals")
    else:
        print()

    # Lookahead first and loud. Every number below it is void if this fires, and
    # a reader who sees the Sharpe first has already been misled.
    if report.lookahead_violations:
        print(
            f"\n  !! LOOKAHEAD DETECTED on {len(report.lookahead_violations)} sampled bar(s): "
            f"{report.lookahead_violations[:8]}\n"
            "     This strategy's signals change when future bars are removed.\n"
            "     Every metric below is meaningless until that is fixed.",
            file=sys.stderr,
        )

    if report.ruined_at_bar is not None:
        print(
            f"\n  !! ACCOUNT WIPED OUT at bar {report.ruined_at_bar} "
            f"({report.timestamps[report.ruined_at_bar]:%Y-%m-%d}).\n"
            "     A compounding position lost more than 100% in a single bar, so\n"
            "     trading stopped there. Every figure below describes a dead\n"
            "     account, not a strategy worth tuning.",
            file=sys.stderr,
        )

    print(f"\n  {'Total return':<22}{m.total_return_pct:>10.2f} %")
    print(f"  {'CAGR':<22}{m.cagr_pct:>10.2f} %")
    print(f"  {'Sharpe':<22}{m.sharpe:>10.3f}")
    print(f"  {'Sortino':<22}{m.sortino:>10.3f}")
    print(f"  {'Calmar':<22}{m.calmar:>10.3f}")
    print(f"  {'Max drawdown':<22}{m.max_drawdown_pct:>10.2f} %")
    print(f"  {'Annual volatility':<22}{m.annual_volatility_pct:>10.2f} %")
    print(f"\n  {'Trades':<22}{m.trades:>10d}")
    print(f"  {'Win rate':<22}{m.win_rate_pct:>10.2f} %")
    profit_factor = f"{m.profit_factor:.3f}" if m.profit_factor is not None else "n/a (no losses)"
    print(f"  {'Profit factor':<22}{profit_factor:>10}")
    print(f"  {'Expectancy / trade':<22}{m.expectancy:>10.2f}")
    print(f"  {'Best / worst trade':<22}{m.best_trade:>10.2f} / {m.worst_trade:.2f}")

    if args.trades and report.trades:
        print(f"\n  {'entry':<12}{'exit':<12}{'side':<7}{'entry px':>11}{'exit px':>11}{'pnl':>11}")
        for trade in report.trades[-args.trades :]:
            print(
                f"  {trade.entry_ts:%Y-%m-%d}  {trade.exit_ts:%Y-%m-%d}  "
                f"{trade.direction:<7}{trade.entry_price:>10.2f} {trade.exit_price:>10.2f} "
                f"{trade.pnl:>10.2f}" + ("  (open)" if trade.open else "")
            )

    print(f"\n  {report.disclaimer}\n")
    return 0


def cmd_terminal(args: argparse.Namespace) -> int:
    """Chat with an AI that drives the engine's tools, using YOUR API key.

    The key comes from `--api-key`, or `$LLM_API_KEY` in the environment. It is
    used for the request and never written anywhere.
    """
    from alpha_engine.narrative.agent import ask, summarize_for_terminal
    from alpha_engine.narrative.providers import list_providers

    api_key = args.api_key or os.environ.get("LLM_API_KEY", "")
    if not api_key:
        print(
            "No API key. The terminal uses YOUR key — the engine never holds one.\n\n"
            "  alpha-engine terminal --provider openai --api-key sk-...\n"
            "  export LLM_API_KEY=sk-...   (or put it in .env)\n\nProviders:",
            file=sys.stderr,
        )
        for entry in list_providers():
            print(
                f"  {entry['key']:<12}{entry['label']}\n"
                f"  {'':<12}default model: {entry['default_model']}\n"
                f"  {'':<12}keys: {entry['keys_url']}",
                file=sys.stderr,
            )
        return 2

    history: list[object] = []

    def turn(question: str) -> None:
        reply = ask(
            question,
            api_key=api_key,
            provider_key=args.provider,
            model=args.model,
            history=history,
        )
        print()
        print(summarize_for_terminal(reply))
        print()
        if not reply.error and reply.answer:
            history.append({"role": "user", "content": question})
            history.append({"role": "assistant", "content": reply.answer})

    if args.question:
        turn(" ".join(args.question))
        return 0

    print("Alpha Engine terminal. Every number comes from a tool, never the model.")
    print("Ctrl-D or 'exit' to quit.\n")
    while True:
        try:
            question = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if question in ("exit", "quit"):
            return 0
        if question:
            turn(question)


def _ic_cell(value: float | None) -> str:
    return f"{value:+.4f}" if value is not None else "   none"


def cmd_factors(args: argparse.Namespace) -> int:
    """Rank the factor registry by measured predictive power for one asset.

    Every factor in `quant/factors.py` is scored by its rank IC against forward
    returns. The table is sorted by |IC|, so the top rows are what actually
    moved with the future on this asset — including, often, nothing.
    """

    from alpha_engine.quant.factors import (
        FACTOR_REGISTRY,
        compute_panel,
        factor_clusters,
        factor_families,
        factor_names,
        flag_low_signal,
    )
    from alpha_engine.quant.ranking import FactorScore, noise_floor_ic, rank_factors

    market = detect_market(args.asset, getattr(args, "market", None))
    cache = Cache()

    if market is Market.IN_FNO:
        print("[error] factor ranking requires a price series (not F&O)", file=sys.stderr)
        return 1

    families = getattr(args, "family", None)
    known = set(factor_families())
    if families:
        unknown = sorted(set(families) - known)
        if unknown:
            print(
                f"[error] unknown factor family: {', '.join(unknown)}\n"
                f"        available: {', '.join(sorted(known))}",
                file=sys.stderr,
            )
            return 1

    selected = factor_names(families=families, include_slow=getattr(args, "all_factors", False))

    series = _load_series(args.asset, market, args.days, getattr(args, "no_refresh", False), cache)
    if not series or not series.candles:
        print(f"[error] no data for {args.asset}", file=sys.stderr)
        return 1

    print(
        f"[ranking] scoring {len(selected)} factors over {len(series.candles)} bars "
        f"of {args.asset}...",
        file=sys.stderr,
    )

    panel = compute_panel(series, names=selected)
    scores = rank_factors(series, panel, horizon=args.horizon)
    low_signal = flag_low_signal(scores)

    # Multiple-testing floor: with this many factors and this little data, what
    # would the best *random* factor have scored? Anything below the line is
    # indistinguishable from noise, however good the t-stat looks.
    obs = [s.n_obs for s in scores if s.n_obs > 0]
    median_obs = sorted(obs)[len(obs) // 2] if obs else 0
    floor = noise_floor_ic(len(scores), median_obs)

    # Per-factor floor. Coverage varies hugely across the registry (a 252-period
    # factor is computable on ~14% of a 366-bar series, a 10-period one on ~97%),
    # and the floor scales with 1/sqrt(n). One median-derived line is therefore
    # too lenient for exactly the long-window factors that crowd the top of the
    # ranking, so every factor is judged against its own sample size.
    own_floor = {s.name: noise_floor_ic(len(scores), s.n_obs) for s in scores}

    def _below_own_floor(s: FactorScore) -> bool:
        f = own_floor.get(s.name)
        return f is not None and s.rank_ic is not None and abs(s.rank_ic) < f

    top_score = next((s for s in scores if s.rank_ic is not None), None)
    best_ic = abs(top_score.rank_ic) if top_score is not None else None
    top_floor = own_floor.get(top_score.name) if top_score is not None else None

    top = getattr(args, "top", 0)
    shown = scores[:top] if top and top > 0 else scores

    if getattr(args, "json", False):
        print(
            json.dumps(
                {
                    "asset": args.asset.upper(),
                    "bars": len(series.candles),
                    "horizon": args.horizon,
                    "factors_scored": len(scores),
                    "median_observations": median_obs,
                    "noise_floor_ic": round(floor, 4) if floor is not None else None,
                    "best_ic_clears_noise_floor": (
                        bool(best_ic >= top_floor)
                        if top_floor is not None and best_ic is not None
                        else None
                    ),
                    "low_signal_families": sorted(f for f, flag in low_signal.items() if flag),
                    "factors": [
                        {
                            "factor": s.name,
                            "family": FACTOR_REGISTRY[s.name].family
                            if s.name in FACTOR_REGISTRY
                            else None,
                            "rank_ic": s.rank_ic,
                            "hit_rate": s.hit_rate,
                            "coverage": round(s.coverage, 3),
                            "n_obs": s.n_obs,
                            "t_stat": s.t_stat,
                            "ic_decay": s.ic_by_horizon,
                            "own_noise_floor_ic": (
                                round(own_floor[s.name], 4)
                                if own_floor.get(s.name) is not None
                                else None
                            ),
                            "clears_own_noise_floor": (
                                None if own_floor.get(s.name) is None else not _below_own_floor(s)
                            ),
                        }
                        for s in shown
                    ],
                },
                indent=2,
            )
        )
        return 0

    header = (
        f"{'factor':<28} {'family':<15} {'rank_ic':>8} {'t_stat':>7} {'hit_rate':>9} "
        f"{'coverage':>9} {'ic(1)':>8} {'ic(5)':>8} {'ic(10)':>8} {'ic(20)':>8} {'vs_floor':>9}"
    )
    print(f"\nFactor ranking for {args.asset.upper()} (horizon={args.horizon} bars)")
    print(f"{len(scores)} factors scored over {len(series.candles)} bars\n")
    print(header)
    print("-" * len(header))
    for s in shown:
        family = FACTOR_REGISTRY[s.name].family if s.name in FACTOR_REGISTRY else "-"
        hr = f"{s.hit_rate:.1%}" if s.hit_rate is not None else "   none"
        cov = f"{s.coverage:.1%}" if s.coverage > 0 else "   none"
        t = f"{s.t_stat:+5.2f}" if s.t_stat is not None else "  none"
        print(
            f"{s.name:<28} {family:<15} {_ic_cell(s.rank_ic):>8} {t:>7} {hr:>9} {cov:>9} "
            f"{_ic_cell(s.ic_by_horizon.get(1)):>8} {_ic_cell(s.ic_by_horizon.get(5)):>8} "
            f"{_ic_cell(s.ic_by_horizon.get(10)):>8} {_ic_cell(s.ic_by_horizon.get(20)):>8} "
            f"{'noise' if _below_own_floor(s) else 'ok':>9}"
        )
    if top and top > 0 and len(scores) > top:
        print(f"\n... {len(scores) - top} more (use --top 0 for all, or --json)")

    if floor is not None:
        print(
            f"\nnoise floor: |IC| >= {floor:.4f} is what the BEST of {len(scores)} purely random "
            f"factors\nwould reach on {median_obs} observations (the median factor's sample). "
            "Each factor\nis judged against its own sample size in the vs_floor column, because "
            "a factor\nwith less data has to clear a higher bar."
        )
        buried = sum(1 for s2 in shown if _below_own_floor(s2))
        if buried:
            print(
                f"  -> {buried} of the {len(shown)} rows shown are marked 'noise': their |IC| "
                f"beats the\n     table-wide line above but not the floor for their own coverage."
            )
        if top_floor is not None and best_ic is not None:
            if best_ic < top_floor:
                print(
                    f"  -> The top factor does NOT clear its own floor ({top_floor:.4f} on "
                    f"{top_score.n_obs} obs).\n     On this much data, this ranking is consistent "
                    "with every factor being noise.\n     Use more history."
                )
            else:
                print(
                    f"  -> The top factor clears its own floor ({top_floor:.4f} on "
                    f"{top_score.n_obs} obs), which\n     makes it worth a second look — not "
                    "proof. Confirm out-of-sample before believing it."
                )

    noisy = sorted(f for f, flag in low_signal.items() if flag)
    if noisy:
        print(
            f"\nlow_signal families (no factor reached |IC| >= 0.02): {', '.join(noisy)}\n"
            "  These are kept for completeness, not because they measured useful."
        )

    if getattr(args, "clusters", False):
        clusters = factor_clusters(panel)
        multi = [c for c in clusters if len(c) > 1]
        print(f"\nCorrelation clusters: {len(clusters)} independent groups among {len(panel)}")
        print("(factors inside one group move together — they are one idea, not many)\n")
        for c in sorted(multi, key=len, reverse=True)[:15]:
            print(f"  [{len(c):>3}] {c[0]}  +  {', '.join(c[1:6])}{' ...' if len(c) > 6 else ''}")

    print()
    return 0


def cmd_risk(args: argparse.Namespace) -> int:
    """Portfolio risk report: position sizing, VaR/CVaR, concentration, regime gate.

    Reads the latest recorded signals and cached prices, then produces a
    risk context report. All outputs are research context, not trading
    instructions.
    """
    from alpha_engine.analyzers.risk import build_risk_report
    from alpha_engine.quant.models import fit_hmm
    from alpha_engine.validation.recorder import read_records

    records = read_records()
    if not records:
        print("[risk] no recorded signals yet; run `scan` first.", file=sys.stderr)
        return 0

    # Latest signal per asset
    latest: dict[str, Any] = {}
    for record in records:
        asset = record.signal.asset
        existing = latest.get(asset)
        if existing is None or record.recorded_at > existing.recorded_at:
            latest[asset] = record

    signals = [rec.signal for rec in latest.values()]

    # Load cached series for each asset
    cache = Cache()
    series_by_asset: dict[str, Any] = {}
    for signal in signals:
        series, _stale = cache.get_price(signal.asset, "1d")
        if series is not None:
            series_by_asset[signal.asset] = series

    # Fit HMM on the broadest available series for regime gate
    hmm = None
    longest_series = max(series_by_asset.values(), key=lambda s: len(s.candles), default=None)
    if longest_series is not None and len(longest_series.candles) >= 40:
        closes = [c.close for c in longest_series.candles]
        actual_rets = [
            (closes[i] / closes[i - 1]) - 1.0 for i in range(1, len(closes)) if closes[i - 1] > 0
        ]
        if len(actual_rets) >= 40:
            hmm = fit_hmm(actual_rets)

    report = build_risk_report(signals, series_by_asset, hmm=hmm)

    if getattr(args, "json", False):
        print(report.model_dump_json(indent=2))
    else:
        _render_risk_text(report)

    return 0


def _render_risk_text(report: Any) -> None:
    """Human-readable risk report block."""
    lines: list[str] = ["Portfolio Risk Report", "=" * 22, ""]

    # Regime gate
    lines.append(
        f"Regime Gate:      {report.regime_gate} (confidence: {report.regime_confidence:.0%})"
    )
    lines.append(f"Risk Score:       {report.risk_score}/100 (100 = minimal risk)")
    lines.append("")

    # Position sizing
    if report.position_sizes:
        lines.append("Position Sizing (inverse-volatility)")
        lines.append("-" * 40)
        for ps in report.position_sizes:
            lines.append(
                f"  {ps.asset:<8} weight={ps.weight:.1%}  "
                f"daily_vol={ps.daily_vol:.2%}  ann_vol={ps.annualized_vol:.1%}"
            )
        lines.append("")

    # Tail risk
    if report.tail_risks:
        lines.append("Tail Risk (95% confidence, trailing 60 bars)")
        lines.append("-" * 40)
        for tr in report.tail_risks:
            lines.append(
                f"  {tr.asset:<8} VaR={tr.var_95:+.2%}  CVaR={tr.cvar_95:+.2%}  "
                f"max_dd={tr.max_drawdown:+.2%}  cur_dd={tr.current_drawdown:+.2%}"
            )
        lines.append("")

    # Concentration
    if report.concentration_warnings:
        lines.append("Concentration Warnings")
        lines.append("-" * 40)
        for w in report.concentration_warnings:
            lines.append(f"  {w}")
        lines.append("")

    lines.append("Research output only, not investment advice.")
    print("\n".join(lines))


def cmd_calibrate(args: argparse.Namespace) -> int:
    """Offline calibration: compute per-analyzer reliability from recorded signals.

    Reads the signal log, scores each signal against cached prices, groups
    by analyzer name, applies Bayesian shrinkage, and optionally writes the
    result to data/calibration.json. Deliberately offline and human-invoked.
    """
    from alpha_engine.validation.calibrate import (
        calibrate,
        calibrate_from_backtest,
        write_calibration,
    )

    if args.from_backtest:
        # The live log needs ten trading days before one swing signal resolves,
        # so a fresh install has nothing to learn from for weeks. Replaying
        # cached history gives thousands of scored calls in seconds instead.
        from alpha_engine.orchestrator import _load_targets_from_defaults

        # The same portfolio `scan-all` and `batch` use, so calibration is
        # measured on exactly the assets the engine actually runs on.
        assets = {t.asset: t.market for t in _load_targets_from_defaults()}
        result = calibrate_from_backtest(
            assets,
            step=args.step,
            min_samples=args.min_samples,
            shrinkage_k=args.shrinkage_k,
        )
        print(
            f"[calibrate] replayed {len(result.assets)} asset(s): {', '.join(result.assets)}",
            file=sys.stderr,
        )
        print(
            "[calibrate] source=backtest — IN SAMPLE on the same bars the analyzers\n"
            "            will run on again. An honest prior, not evidence. Re-run\n"
            "            without --from-backtest once real signals have resolved.",
            file=sys.stderr,
        )
    else:
        result = calibrate(
            min_samples=args.min_samples,
            shrinkage_k=args.shrinkage_k,
        )

    if args.dry_run:
        print(result.model_dump_json(indent=2))
        return 0

    path = write_calibration(result)
    print(f"[calibrate] wrote {len(result.analyzers)} analyzer reliabilities to {path}")
    print(f"[calibrate] window: {result.window_records} records, {result.window_resolved} resolved")
    if any(a.used_default for a in result.analyzers):
        defaults = [a.name for a in result.analyzers if a.used_default]
        print(f"[calibrate] below min_samples ({args.min_samples}), kept default 0.50: {defaults}")

    return 0


def _add_market_args(sub: argparse.ArgumentParser, default_days: int) -> None:
    sub.add_argument("asset", help="symbol, e.g. BTC, ETH, AAPL, NIFTY")
    sub.add_argument(
        "--market",
        choices=[
            Market.CRYPTO.value,
            Market.US_EQUITY.value,
            Market.IN_EQUITY.value,
            Market.IN_FNO.value,
            Market.FOREX.value,
        ],
        default=None,
        help="force the market instead of auto-detecting",
    )
    sub.add_argument("--days", type=int, default=default_days, help="history window to fetch")
    sub.add_argument("--no-refresh", action="store_true", help="use cache even if stale")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="alpha-engine", description="Open research engine for market signals."
    )
    sub = p.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="generate a signal for one asset")
    _add_market_args(scan, default_days=90)
    scan.add_argument("--no-record", action="store_true", help="do not append to the signal log")
    scan.add_argument(
        "--llm", action="store_true", help="use optional LLM to rephrase thesis (needs LLM_API_KEY)"
    )
    scan.set_defaults(func=cmd_scan)

    scan_chain = sub.add_parser(
        "scan-chain",
        help="generate an F&O signal from a normalized OptionsChain JSON fixture",
    )
    scan_chain.add_argument("chain_file", help="path to a normalized OptionsChain JSON file")
    scan_chain.add_argument(
        "--underlying",
        default=None,
        help="override the underlying symbol when the raw payload omits it",
    )
    scan_chain.add_argument(
        "--no-record", action="store_true", help="do not append to the signal log"
    )
    scan_chain.set_defaults(func=cmd_scan_chain)

    fetch_chain = sub.add_parser(
        "fetch-chain",
        help="fetch a live Indian options chain from Breeze or Angel One and analyze it",
    )
    fetch_chain.add_argument("asset", help="underlying symbol, e.g. NIFTY")
    fetch_chain.add_argument(
        "--expiry",
        required=True,
        help="expiry date to fetch, in YYYY-MM-DD format",
    )
    fetch_chain.add_argument(
        "--broker",
        choices=["breeze", "angelone", "dhan"],
        default="breeze",
        help="broker to fetch from (default: breeze)",
    )
    fetch_chain.add_argument(
        "--no-record", action="store_true", help="do not append to the signal log"
    )
    fetch_chain.set_defaults(func=cmd_fetch_chain)

    watch = sub.add_parser(
        "watch",
        help="scan multiple assets and print a compact table",
    )
    watch.add_argument("assets", nargs="+", help="symbols to scan, e.g. BTC AAPL NIFTY")
    watch.add_argument(
        "--market",
        choices=[m.value for m in Market],
        default=None,
        help="force one market for every asset",
    )
    watch.add_argument("--days", type=int, default=90, help="history window to fetch")
    watch.add_argument("--no-refresh", action="store_true", help="use cache even if stale")
    watch.add_argument("--record", action="store_true", help="append results to the signal log")
    watch.add_argument(
        "--llm", action="store_true", help="use optional LLM to rephrase thesis (needs LLM_API_KEY)"
    )
    watch.add_argument(
        "--sort",
        choices=["asset", "market", "confidence"],
        default=None,
        help="sort the batch output before printing",
    )
    watch.set_defaults(func=cmd_watch)

    bt = sub.add_parser("backtest", help="replay history through the analyzer, no lookahead")
    _add_market_args(bt, default_days=365)
    bt.add_argument("--step", type=int, default=1, help="bars between simulated signals")
    bt.add_argument(
        "--options",
        action="store_true",
        help="backtest the ATM option matching each signal (model-priced) alongside the underlying",
    )
    bt.add_argument(
        "--per-analyzer",
        action="store_true",
        help="backtest each analyzer in isolation plus the blend, for comparison",
    )
    bt.set_defaults(func=cmd_backtest)

    trade = sub.add_parser("trade", help="scan an asset and place a (paper) order from the signal")
    _add_market_args(trade, default_days=90)
    trade.add_argument(
        "--option", action="store_true", help="trade the ATM option instead of the underlying"
    )
    trade.add_argument("--qty", type=int, default=1, help="order quantity (units/lots)")
    trade.add_argument(
        "--strike-step", type=float, default=50.0, help="strike grid for ATM rounding (NIFTY=50)"
    )
    trade.add_argument("--expiry", default=None, help="option expiry, YYYY-MM-DD")
    trade.add_argument(
        "--product", choices=["intraday", "delivery"], default="intraday", help="product type"
    )
    trade.add_argument("--broker", choices=["dhan", "angelone"], default="dhan", help="live broker")
    trade.set_defaults(func=cmd_trade)

    webhook = sub.add_parser(
        "webhook", help="run the inbound trade webhook (paper unless LIVE_TRADING=1)"
    )
    webhook.add_argument("--host", default="127.0.0.1", help="bind address")
    webhook.add_argument("--port", type=int, default=8787, help="port")
    webhook.set_defaults(func=cmd_webhook)

    report = sub.add_parser(
        "report", help="full quant metrics report (regime, scores, vol forecast, indicators)"
    )
    _add_market_args(report, default_days=365)
    report.add_argument("--json", action="store_true", help="emit the full report as JSON")
    report.set_defaults(func=cmd_report)

    stats = sub.add_parser("record-stats", help="score recorded live signals against outcomes")
    stats.set_defaults(func=cmd_record_stats)

    scan_all = sub.add_parser("scan-all", help="scan all configured assets across all markets")
    scan_all.add_argument("--config", default=None, help="path to portfolio.json config file")
    scan_all.add_argument(
        "--assets", nargs="+", default=None, help="explicit asset list, e.g. BTC AAPL NIFTY"
    )
    scan_all.add_argument("--days", type=int, default=90, help="history window to fetch")
    scan_all.add_argument(
        "--no-record", action="store_true", help="do not append to the signal log"
    )
    scan_all.add_argument("--llm", action="store_true", help="use optional LLM to rephrase thesis")
    scan_all.set_defaults(func=cmd_scan_all)

    batch = sub.add_parser("batch", help="scheduled batch scan with report output (cron-friendly)")
    batch.add_argument("--config", default=None, help="path to portfolio.json config file")
    batch.add_argument("--assets", nargs="+", default=None, help="explicit asset list")
    batch.add_argument("--days", type=int, default=90, help="history window to fetch")
    batch.add_argument("--no-record", action="store_true", help="do not append to the signal log")
    batch.add_argument("--llm", action="store_true", help="use optional LLM to rephrase thesis")
    batch.add_argument("--output", default=None, help="write JSON report to this path")
    batch.set_defaults(func=cmd_batch)

    ingest = sub.add_parser(
        "ingest",
        help="refresh cached context data (news, on-chain, fundamentals)",
    )
    ingest.add_argument("assets", nargs="*", help="assets to fetch context for")
    ingest.add_argument(
        "--kind",
        action="append",
        choices=["news", "onchain", "fundamentals", "events"],
        help="refresh only this kind (repeatable); default is whatever is stale",
    )
    ingest.add_argument("--force", action="store_true", help="refresh even if fresh")
    ingest.add_argument("--json", action="store_true", help="emit the report as JSON")
    ingest.add_argument(
        "--strict",
        action="store_true",
        help="exit non-zero if any data kind returned zero items (for scheduled runs)",
    )
    ingest.set_defaults(func=cmd_ingest)

    health_cmd = sub.add_parser(
        "health",
        help="report which data sources are working and which have gone quiet",
    )
    health_cmd.add_argument("--json", action="store_true", help="emit the report as JSON")
    health_cmd.add_argument(
        "--strict",
        action="store_true",
        help="exit non-zero when any source is degraded (for cron alerting)",
    )
    health_cmd.set_defaults(func=cmd_health)

    orch = sub.add_parser(
        "orchestrate",
        help="event-driven run: news triggers targeted re-scans, priority-ordered",
    )
    orch.add_argument("assets", nargs="*", help="override the configured portfolio")
    orch.add_argument("--config", help="portfolio JSON file")
    orch.add_argument("--days", type=int, default=90, help="history per scan")
    orch.add_argument("--news", action="store_true", help="build triggers from recent headlines")
    orch.add_argument(
        "--news-only",
        action="store_true",
        help="skip the routine sweep; run only news-driven triggers",
    )
    orch.add_argument(
        "--news-age", type=float, default=6.0, help="headline age cutoff in hours (default: 6)"
    )
    orch.add_argument("--no-record", action="store_true", help="do not append to the signal log")
    orch.add_argument(
        "--no-refresh-context", action="store_true", help="skip the freshness ingestion pass"
    )
    orch.add_argument("--json", action="store_true", help="emit the report as JSON")
    orch.add_argument("--output", help="also write the JSON report to this path")
    orch.set_defaults(func=cmd_orchestrate)

    dashboard = sub.add_parser(
        "dashboard", help="launch the web app: dashboard + AI terminal + HTTP/MCP API"
    )
    dashboard.add_argument("--host", default="127.0.0.1", help="bind address")
    dashboard.add_argument("--port", type=int, default=8000, help="port to listen on")
    dashboard.add_argument(
        "--allow-writes",
        action="store_true",
        help="permit API callers to append to the signal log (default: read-only)",
    )
    dashboard.add_argument(
        "--rate-limit", type=int, default=None, help="API requests per minute per IP (0 disables)"
    )
    dashboard.add_argument("--cors", metavar="ORIGIN", help="send CORS headers for this origin")
    dashboard.add_argument(
        "--allow-public",
        action="store_true",
        help="bind a non-loopback address without an API key (containers)",
    )
    dashboard.add_argument(
        "--log-requests",
        action="store_true",
        help="one JSON line per request; never bodies, query values or keys",
    )
    dashboard.set_defaults(func=cmd_dashboard)

    strategies = sub.add_parser("strategies", help="list strategies available to strategy-backtest")
    strategies.set_defaults(func=cmd_strategies)

    sbt = sub.add_parser(
        "strategy-backtest",
        help="trade-level backtest of one strategy: equity curve, trades, Sharpe",
        description=(
            "Backtest a trading rule as a position book. Unlike `backtest` (which "
            "scores the engine's own signals for directional accuracy), this "
            "reports what the money would have done: entries, exits, transaction "
            "costs, drawdown and Sharpe. Pass --option to require every signal to "
            "be confirmed on a real option price series before it becomes a trade."
        ),
    )
    sbt.add_argument("asset", help="underlying ticker, e.g. NIFTY, BTC, AAPL")
    sbt.add_argument(
        "--strategy", required=True, help="strategy class name (see `alpha-engine strategies`)"
    )
    sbt.add_argument(
        "--param",
        action="append",
        metavar="NAME=VALUE",
        help="override a strategy parameter; repeatable",
    )
    sbt.add_argument("--market", help="override market auto-detection")
    sbt.add_argument("--days", type=int, default=365, help="history window (default 365)")
    sbt.add_argument("--option", help="ticker of an option series to cross-verify signals against")
    sbt.add_argument(
        "--csv",
        metavar="PATH",
        help="backtest your own OHLCV CSV instead of fetching (columns: "
        "datetime,open,high,low,close[,volume])",
    )
    sbt.add_argument(
        "--option-csv",
        metavar="PATH",
        help="the option contract's own CSV, to cross-verify every signal against",
    )
    sbt.add_argument(
        "--interval",
        choices=[i.value for i in Interval],
        default=Interval.DAY.value,
        help="bar interval of the CSV data; sets the Sharpe annualisation "
        "(5-minute bars annualised as daily overstate Sharpe ~9x)",
    )
    sbt.add_argument(
        "--trade-on",
        choices=["underlying", "option"],
        default="underlying",
        help="which leg P&L accrues on (default underlying)",
    )
    sbt.add_argument(
        "--no-confirmation",
        action="store_true",
        help="take every signal without option confirmation",
    )
    sbt.add_argument("--capital", type=float, default=100_000.0, help="starting capital")
    sbt.add_argument(
        "--cost-bps", type=float, default=2.0, help="round-trip transaction cost in basis points"
    )
    sbt.add_argument(
        "--trades", type=int, default=0, metavar="N", help="also print the last N trades"
    )
    sbt.add_argument("--json", action="store_true", help="emit the full report as JSON")
    sbt.add_argument("--no-refresh", action="store_true", help="use cached prices only")
    sbt.set_defaults(func=cmd_strategy_backtest)

    terminal = sub.add_parser(
        "terminal",
        help="chat with an AI that drives the engine's tools (bring your own key)",
        description=(
            "An AI research terminal. You supply the LLM API key; the model answers "
            "by calling this engine's deterministic tools and relaying what they "
            "returned. It is not permitted to compute or recall a number itself, "
            "and every call it makes is printed above its answer so you can check "
            "any figure it quotes."
        ),
    )
    terminal.add_argument("question", nargs="*", help="ask once and exit; omit for a REPL")
    terminal.add_argument(
        "--provider", default="openai", help="openai | anthropic | openrouter | groq"
    )
    terminal.add_argument("--model", help="override the provider's default model")
    terminal.add_argument("--api-key", help="your LLM API key (or set LLM_API_KEY)")
    terminal.set_defaults(func=cmd_terminal)

    factors = sub.add_parser(
        "factors",
        help="factor ranking: which features predict forward returns",
    )
    _add_market_args(factors, default_days=365)
    factors.add_argument("--horizon", type=int, default=10, help="forward return horizon in bars")
    factors.add_argument("--json", action="store_true", help="emit the full ranking as JSON")
    factors.add_argument(
        "--top", type=int, default=40, help="show only the top N rows (0 = all, default: 40)"
    )
    factors.add_argument(
        "--family",
        action="append",
        help="restrict to one factor family (repeatable), e.g. --family momentum",
    )
    factors.add_argument(
        "--all-factors",
        action="store_true",
        help="include slow model-fitting factors (GARCH/HMM); much slower",
    )
    factors.add_argument(
        "--clusters",
        action="store_true",
        help="also report correlation clusters (which factors are the same idea)",
    )
    factors.set_defaults(func=cmd_factors)

    risk = sub.add_parser(
        "risk",
        help="portfolio risk report: position sizing, VaR/CVaR, concentration, regime gate",
    )
    risk.add_argument("--json", action="store_true", help="emit the full report as JSON")
    risk.set_defaults(func=cmd_risk)

    calibrate = sub.add_parser(
        "calibrate",
        help="compute per-analyzer reliability from recorded signals (offline, human-invoked)",
    )
    calibrate.add_argument(
        "--min-samples",
        type=int,
        default=50,
        help="minimum resolved signals per analyzer to override default (default: 50)",
    )
    calibrate.add_argument(
        "--from-backtest",
        action="store_true",
        help="replay cached history instead of waiting for live signals to resolve "
        "(IN SAMPLE — an honest prior, not evidence)",
    )
    calibrate.add_argument(
        "--step",
        type=int,
        default=1,
        help="bars between replayed signals when using --from-backtest (default: 1)",
    )
    calibrate.add_argument(
        "--shrinkage-k",
        type=float,
        default=30.0,
        help="Bayesian shrinkage parameter (default: 30)",
    )
    calibrate.add_argument(
        "--dry-run",
        action="store_true",
        help="print the calibration result without writing to disk",
    )
    calibrate.set_defaults(func=cmd_calibrate)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
