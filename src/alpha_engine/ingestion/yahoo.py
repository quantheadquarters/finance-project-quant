"""Yahoo Finance ingestion adapter for US equity daily candles. Chosen over the
plan's original Finnhub suggestion because Yahoo's chart endpoint needs no API
key, which keeps the equity path as zero-setup as the crypto one. (Finnhub's
free tier no longer includes stock candles; Stooq now sits behind a JavaScript
challenge. Verified at build time.)

Same contract as every ingestion adapter: pull, normalize into cache models,
write to the cache. Zero analysis. Analyzers never import this module.

Yahoo's endpoint is unofficial-but-stable and requires a browser-ish
User-Agent. Be a polite guest: the cache TTL means one fetch a day per symbol.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from alpha_engine import health, net
from alpha_engine.cache.interface import Cache
from alpha_engine.cache.models import Candle, Interval, PriceSeries

_BASE = "https://query1.finance.yahoo.com/v8/finance/chart"
SOURCE = "yahoo"
_HEADERS = {"User-Agent": "Mozilla/5.0 (alpha-engine; research/education tool)"}


def _parse_chart(payload: dict) -> list[Candle]:
    """Normalize Yahoo's chart JSON into candles. Pure function so the parsing
    rules are unit-testable without the network. Bars with a null close (halts,
    partial rows Yahoo sometimes emits) are dropped rather than guessed at."""
    chart = payload.get("chart", {})
    error = chart.get("error")
    if error:
        raise ValueError(f"Yahoo chart error: {error.get('description', error)}")
    results = chart.get("result") or []
    if not results:
        raise ValueError("Yahoo chart returned no result")

    result = results[0]
    timestamps = result.get("timestamp") or []
    quote = (result.get("indicators", {}).get("quote") or [{}])[0]
    opens = quote.get("open") or []
    highs = quote.get("high") or []
    lows = quote.get("low") or []
    closes = quote.get("close") or []
    volumes = quote.get("volume") or []

    candles: list[Candle] = []
    for i, ts in enumerate(timestamps):
        if i >= len(closes) or closes[i] is None:
            continue
        close = closes[i]
        candles.append(
            Candle(
                ts=datetime.fromtimestamp(ts, tz=timezone.utc),
                open=opens[i] if i < len(opens) and opens[i] is not None else close,
                high=highs[i] if i < len(highs) and highs[i] is not None else close,
                low=lows[i] if i < len(lows) and lows[i] is not None else close,
                close=close,
                volume=volumes[i] if i < len(volumes) else None,
            )
        )
    return candles


def fetch_daily(asset: str, days: int = 365, cache: Cache | None = None) -> PriceSeries:
    """Fetch daily OHLCV for an equity symbol, normalize, cache, and return it.

    Uses `get_with_retry` rather than a bare `get`, because Yahoo rate-limits by
    IP and answers **HTTP 429** when it does — observed both from a GitHub
    runner and from a residential connection after a burst of scans. A bare GET
    turns that transient throttle into a failed scan for every equity in the
    batch, and the retry helper (which honours `Retry-After`) already existed in
    `net.py` for exactly this; the Indian broker adapters were using it and this
    one was not.

    Price is also the only data kind with no alternative source. Every keyless
    equity API checked on 2026-07-26 — Stooq on both its .com and .pl domains —
    now answers with a JavaScript proof-of-work challenge served at HTTP 200,
    which is worse than a refusal because a naive client caches the challenge
    page as data. So the strategy here is to survive the throttle rather than
    route around it, and to record health so a real outage is visible instead of
    looking like a quiet market.
    """
    cache = cache or Cache()
    asset = asset.upper()

    now = int(time.time())
    try:
        resp = net.get_with_retry(
            f"{_BASE}/{asset}",
            params={
                "period1": str(now - days * 86400),
                "period2": str(now),
                "interval": "1d",
                "events": "history",
            },
            headers=_HEADERS,
            timeout=20,
        )
        resp.raise_for_status()
        payload = resp.json()
        candles = _parse_chart(payload)
        results = payload.get("chart", {}).get("result") or [{}]
        meta = results[0].get("meta", {})
        currency = meta.get("currency") if isinstance(meta, dict) else None
    except Exception as e:  # noqa: BLE001 - recorded, then re-raised for the caller
        health.record(f"price.{SOURCE}", error=f"{type(e).__name__}: {e}")
        raise

    # Recorded per source so a dead price feed is as visible as a dead news
    # feed. Price had no health record at all before this, which meant the most
    # important input in the engine was the least observable.
    health.record(f"price.{SOURCE}", items=len(candles))

    series = PriceSeries(
        asset=asset,
        interval=Interval.DAY,
        candles=candles,
        source=SOURCE,
        currency=currency if isinstance(currency, str) else None,
    )
    cache.put_price(series)
    return series
