"""Read-only dashboard data assembly.

The web layer should stay paper-thin. It asks for one payload and renders it;
this module gathers the latest records, scores them against cached prices, and
returns JSON-friendly data structures.

Thread safety: build_dashboard_payload is guarded by a lock so that concurrent
requests from ThreadingHTTPServer see a consistent snapshot — the signal log
is read and scored atomically, not interleaved with another request's writes.
"""

from __future__ import annotations

import threading
from collections import defaultdict
from pathlib import Path
from typing import Any

from alpha_engine.analyzers.portfolio_signal import build_portfolio_view
from alpha_engine.cache.interface import Cache
from alpha_engine.cache.models import PriceSeries
from alpha_engine.validation.outcomes import (
    annotate_coverage,
    score_record,
    summarize_outcomes,
)
from alpha_engine import __version__
from alpha_engine.validation.recorder import SignalRecord, read_records

# Serialize dashboard builds so concurrent HTTP requests see a consistent snapshot.
_build_lock = threading.Lock()


def latest_records(records: list[SignalRecord]) -> list[SignalRecord]:
    """Return the newest record per asset, newest first."""
    latest: dict[str, SignalRecord] = {}
    for record in records:
        asset = record.signal.asset
        existing = latest.get(asset)
        if existing is None or record.recorded_at > existing.recorded_at:
            latest[asset] = record
    return sorted(latest.values(), key=lambda r: r.recorded_at, reverse=True)


def build_dashboard_payload(
    records_root: str | Path | None = None, cache: Cache | None = None
) -> dict[str, Any]:
    """Assemble the current dashboard state.

    The payload is intentionally JSON-friendly so the web layer can serve it as
    either HTML or API output without duplicating logic.
    """
    cache = cache or Cache()
    with _build_lock:
        records = read_records(records_root)
        latest = latest_records(records)

        # One disk read per asset, not per record: with thousands of records
        # per asset, re-reading the same price JSON dominated request time.
        from alpha_engine.validation.outcomes import Outcome

        series_cache: dict[str, PriceSeries | None] = {}
        scored: list[tuple[float, Outcome]] = []
        missing_assets: set[str] = set()
        for record in records:
            asset = record.signal.asset
            if asset not in series_cache:
                series_cache[asset] = cache.get_price(asset, "1d")[0]
            series = series_cache[asset]
            if series is None:
                missing_assets.add(asset)
                continue
            scored.append((record.signal.confidence, score_record(record, series)))

        by_market: dict[str, int] = defaultdict(int)
        for record in latest:
            by_market[record.signal.market.value] += 1

        # Portfolio view: aggregate the latest signal per asset, with return
        # correlations for the assets whose prices are cached.
        series_by_asset: dict[str, PriceSeries] = {}
        for record in latest:
            asset = record.signal.asset
            if asset not in series_cache:
                series_cache[asset] = cache.get_price(asset, "1d")[0]
            cached = series_cache[asset]
            if cached is not None:
                series_by_asset[asset] = cached
        portfolio = build_portfolio_view([r.signal for r in latest], series_by_asset)

        # Risk report: position sizing, VaR/CVaR, concentration, regime gate
        from alpha_engine.analyzers.risk import build_risk_report
        from alpha_engine.quant.models import fit_hmm

        risk_signals = [r.signal for r in latest]
        hmm = None
        longest = max(series_by_asset.values(), key=lambda s: len(s.candles), default=None)
        if longest is not None and len(longest.candles) >= 40:
            closes = [c.close for c in longest.candles]
            actual_rets = [
                (closes[i] / closes[i - 1]) - 1.0
                for i in range(1, len(closes))
                if closes[i - 1] > 0
            ]
            if len(actual_rets) >= 40:
                hmm = fit_hmm(actual_rets)
        risk = build_risk_report(risk_signals, series_by_asset, hmm=hmm)

        return {
            # Served rather than hardcoded in the page. The footer claimed
            # v0.1.0 while running 0.5.0 code, and a version number nobody can
            # trust is worse than none — it is the first thing quoted in a bug
            # report.
            "version": __version__,
            "total_records": len(records),
            "latest_count": len(latest),
            "assets_by_market": dict(sorted(by_market.items())),
            "latest_signals": [
                {
                    "record_id": record.record_id,
                    "asset": record.signal.asset,
                    "market": record.signal.market.value,
                    "direction": record.signal.direction.value,
                    "confidence": record.signal.confidence,
                    "timeframe": record.signal.timeframe.value,
                    "timestamp": record.signal.timestamp.isoformat(),
                    "recorded_at": record.recorded_at.isoformat(),
                    "entry_price": record.entry_price,
                    "invalidation_level": record.signal.invalidation_level,
                    "thesis": record.signal.thesis,
                    "sources": [s.model_dump(mode="json") for s in record.signal.signal_sources],
                }
                for record in latest
            ],
            # Same coverage rule as `record-stats` and the toolkit: an uncached
            # asset drops out of scoring entirely, so the hit rate is withheld
            # rather than shown over a subset. This is the most visible surface
            # of that number — it renders as a headline tile.
            "outcomes": annotate_coverage(
                summarize_outcomes(scored).model_dump(mode="json"),
                total=len(records),
                scored=len(scored),
                missing_assets=missing_assets,
            ),
            "portfolio": portfolio.model_dump(mode="json"),
            "risk": risk.model_dump(mode="json"),
        }


def build_asset_history(
    asset: str, records_root: str | Path | None = None, cache: Cache | None = None
) -> dict[str, Any]:
    """Full recorded signal history for one asset, newest first.

    Each record is scored against cached prices when they exist, so the
    per-asset view can show not just what the engine said but whether it
    was right — the same honesty-first framing as record-stats.
    """
    cache = cache or Cache()
    asset = asset.upper()
    with _build_lock:
        records = [r for r in read_records(records_root) if r.signal.asset == asset]
        series, _stale = cache.get_price(asset, "1d")
    records.sort(key=lambda r: r.recorded_at, reverse=True)

    history: list[dict[str, Any]] = []
    for record in records:
        outcome = score_record(record, series).model_dump(mode="json") if series else None
        history.append(
            {
                "record_id": record.record_id,
                "market": record.signal.market.value,
                "direction": record.signal.direction.value,
                "confidence": record.signal.confidence,
                "timeframe": record.signal.timeframe.value,
                "recorded_at": record.recorded_at.isoformat(),
                "entry_price": record.entry_price,
                "invalidation_level": record.signal.invalidation_level,
                "thesis": record.signal.thesis,
                "sources": [s.model_dump(mode="json") for s in record.signal.signal_sources],
                "outcome": outcome,
            }
        )

    return {"asset": asset, "count": len(history), "history": history}
