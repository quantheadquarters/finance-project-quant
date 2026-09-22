"""Offline, date-matched comparison of price-only and contextual equity signals."""

from __future__ import annotations

import argparse
import json
import math
from datetime import date, datetime, timedelta
from pathlib import Path

from alpha_engine.analyzers.sentiment import MAX_AGE_DAYS
from alpha_engine.cache.interface import Cache
from alpha_engine.cache.models import Fundamentals, NewsItem, PriceSeries
from alpha_engine.schema.signal import Direction, Market, Timeframe
from alpha_engine.validation.backtest import DEFAULT_WARMUP, signal_at
from alpha_engine.validation.outcomes import HORIZON_BARS, OutcomeStatus, score_forward

AAPL_HOLDOUT_START = date(2026, 9, 22)


def compare_context(
    series: PriceSeries,
    fundamentals: list[Fundamentals],
    news: list[NewsItem],
    *,
    step: int = 10,
    cost_bps: float = 10.0,
) -> dict:
    """Score each context addition against its immediate baseline on shared dates.

    This is a signal-level diagnostic, not an executable trading simulation.
    An extra context veto is reported separately rather than silently dropped.
    """
    if step < 1 or not math.isfinite(cost_bps) or cost_bps < 0:
        raise ValueError("step must be positive and cost_bps finite and nonnegative")
    reserved = 0
    if series.asset.upper() == "AAPL":
        development = [c for c in series.candles if c.ts.date() < AAPL_HOLDOUT_START]
        reserved = len(series.candles) - len(development)
        series = series.model_copy(update={"candles": development})
    candles = series.candles
    if len(candles) <= DEFAULT_WARMUP + HORIZON_BARS[Timeframe.SWING]:
        raise ValueError("not enough cached daily candles for a resolved comparison")
    if candles[0].close <= 0:
        raise ValueError("first cached close must be positive")

    unique_news = {}
    for item in news:
        if (
            series.asset.upper() in item.asset_tags
            and candles[0].ts - timedelta(days=MAX_AGE_DAYS) <= item.ts <= candles[-1].ts
        ):
            # The live cache deduplicates by URL; archives bypass that cache.
            key = item.url or f"{item.source}:{item.ts.isoformat()}:{item.headline}"
            unique_news.setdefault(key, item)
    tagged_news = list(unique_news.values())
    modes = {"technical": {}, "technical_fundamental": {}}
    if tagged_news:
        modes["technical_fundamental_news"] = {}
    active = {"fundamentals": 0, "news": 0}
    active_dates = {"fundamentals": set(), "news": set()}
    sampled = 0

    for t in range(DEFAULT_WARMUP, len(candles) - HORIZON_BARS[Timeframe.SWING], step):
        sampled += 1
        for name, kwargs in (
            ("technical", {}),
            ("technical_fundamental", {"fundamentals_data": fundamentals}),
            (
                "technical_fundamental_news",
                {"fundamentals_data": fundamentals, "news_data": tagged_news},
            ),
        ):
            if name not in modes:
                continue
            signal, entry = signal_at(series, t, market=Market.US_EQUITY, **kwargs)
            if name == "technical_fundamental" and any(
                s.name == "fundamentals" and s.weight > 0 for s in signal.signal_sources
            ):
                active["fundamentals"] += 1
                active_dates["fundamentals"].add(t)
            if name == "technical_fundamental_news" and any(
                s.name == "news.sentiment" and s.weight > 0 for s in signal.signal_sources
            ):
                active["news"] += 1
                active_dates["news"].add(t)
            if signal.direction is Direction.NEUTRAL or entry <= 0:
                continue
            outcome = score_forward(
                signal.direction,
                entry,
                signal.invalidation_level,
                candles[t + 1 :],
                HORIZON_BARS[Timeframe.SWING],
            )
            if outcome.status is OutcomeStatus.RESOLVED:
                modes[name][t] = outcome

    if active["news"] == 0:
        modes.pop("technical_fundamental_news", None)
    round_trip_cost = 2 * cost_bps / 10_000

    def summary(results: dict, dates: list[int]) -> dict:
        paired = [results[t] for t in dates]
        returns = [o.realized_return for o in paired if o.realized_return is not None]
        hits = sum(o.hit is True for o in paired)
        return {
            "hits": hits,
            "hit_rate": round(hits / len(paired), 4) if paired else None,
            "avg_captured_move": round(sum(returns) / len(returns), 6) if returns else None,
            "avg_move_after_cost_proxy": round(sum(returns) / len(returns) - round_trip_cost, 6)
            if returns
            else None,
        }

    comparisons = {}
    for label, baseline, candidate in (
        ("fundamentals_vs_technical", "technical", "technical_fundamental"),
        (
            "news_vs_technical_fundamental",
            "technical_fundamental",
            "technical_fundamental_news",
        ),
    ):
        if candidate not in modes:
            continue
        matched = sorted(set(modes[baseline]) & set(modes[candidate]))
        context = "fundamentals" if label.startswith("fundamentals") else "news"
        active_matched = [t for t in matched if t in active_dates[context]]
        comparisons[label] = {
            "matched_resolved": len(matched),
            "matched_dates": [candles[t].ts.isoformat() for t in matched],
            "baseline": summary(modes[baseline], matched),
            "candidate": summary(modes[candidate], matched),
            "context_active_matched": len(active_matched),
            "active_baseline": summary(modes[baseline], active_matched),
            "active_candidate": summary(modes[candidate], active_matched),
            "baseline_resolved_directional": len(modes[baseline]),
            "candidate_resolved_directional": len(modes[candidate]),
        }

    return {
        "asset": series.asset,
        "bars": len(candles),
        "reserved_holdout_bars": reserved,
        "holdout_starts": AAPL_HOLDOUT_START.isoformat()
        if series.asset.upper() == "AAPL"
        else None,
        "first_bar": candles[0].ts.isoformat(),
        "last_bar": candles[-1].ts.isoformat(),
        "step": step,
        "sampled_dates": sampled,
        "cost_bps_per_side": cost_bps,
        "buy_hold_full_sample": round(candles[-1].close / candles[0].close - 1, 6),
        "available_fundamental_periods": sum(
            p.available_at is not None and p.available_at <= candles[-1].ts for p in fundamentals
        ),
        "tagged_news_items": len(tagged_news),
        "fundamentals_active_dates": active["fundamentals"],
        "news_active_dates": active["news"],
        "news_comparison_available": bool(
            comparisons.get("news_vs_technical_fundamental", {}).get("context_active_matched")
        ),
        "comparisons": comparisons,
        "warning": (
            "Research only, not financial advice. Matched calls are correlated and this is not "
            "a trade/account backtest. Cost is a two-sided per-call proxy; buy-and-hold covers "
            "the full sample and is context, not a directly comparable return. Current SEC "
            "facts are not a historical data-vintage archive. No alpha is established."
        ),
    }


def read_news_archive(
    path: Path, asset: str, first_signal_date: date, last_development_date: date
) -> tuple[list[NewsItem], dict]:
    """Validate dated, asset-specific research headlines before they enter a replay."""
    try:
        archive = json.loads(path.read_text())
        if not isinstance(archive, dict) or archive.get("asset") != asset:
            raise ValueError("news archive must be an object for this asset")
        requested_from = date.fromisoformat(archive["requested_from"])
        requested_to = date.fromisoformat(archive["requested_to"])
        retrieved_at = datetime.fromisoformat(archive["retrieved_at"])
        if retrieved_at.tzinfo is None:
            raise ValueError("archive retrieval time must include a timezone")
        if requested_from > requested_to or requested_to > retrieved_at.date():
            raise ValueError("archive request dates must precede retrieval")
        rows = archive["items"]
        if not isinstance(rows, list) or not archive.get("provider"):
            raise ValueError("news archive needs a provider and an items array")
        news = [NewsItem.model_validate(row) for row in rows]
        if any(item.ts.tzinfo is None for item in news):
            raise ValueError("headline times must include a timezone")
        if any(not requested_from <= item.ts.date() <= requested_to for item in news):
            raise ValueError("headline outside archive's requested date range")
        if any(item.ts > retrieved_at for item in news):
            raise ValueError("headline cannot postdate archive retrieval")
        if (
            requested_from > first_signal_date - timedelta(days=MAX_AGE_DAYS)
            or requested_to < last_development_date
        ):
            raise ValueError("news archive request does not cover the development news lookback")
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"invalid news archive: {exc}") from exc
    return news, {
        "provider": archive["provider"],
        "requested_from": archive["requested_from"],
        "requested_to": archive["requested_to"],
        "retrieved_at": archive["retrieved_at"],
        "returned_items": len(news),
        "coverage_note": "Requested dates do not prove provider completeness or historical vintage.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare cached equity signals on matched dates")
    parser.add_argument("asset", nargs="?", default="AAPL")
    parser.add_argument("--step", type=int, default=10)
    parser.add_argument("--cost-bps", type=float, default=10.0)
    parser.add_argument("--news-file", type=Path, help="unpruned NewsItem JSON research archive")
    args = parser.parse_args()
    cache = Cache()
    asset = args.asset.upper()
    series, _ = cache.get_price(asset, "1d")
    if series is None:
        parser.error(f"no cached candles for {asset}; run 'backtest {asset}' first")
    fundamentals, _ = cache.get_fundamentals(asset)
    archive_info = None
    if args.news_file:
        try:
            development = [c for c in series.candles if c.ts.date() < AAPL_HOLDOUT_START]
            news, archive_info = read_news_archive(
                args.news_file,
                asset,
                development[DEFAULT_WARMUP].ts.date(),
                development[-1].ts.date(),
            )
        except (IndexError, ValueError) as exc:
            parser.error(str(exc))
    else:
        news, _ = cache.get_news()
    try:
        result = compare_context(series, fundamentals, news, step=args.step, cost_bps=args.cost_bps)
    except ValueError as exc:
        parser.error(str(exc))
    if archive_info is not None:
        result["news_archive"] = archive_info
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
