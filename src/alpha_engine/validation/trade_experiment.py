"""Fixed next-open execution test for the engine's equity research signals.

This is separate from signal hit-rate scoring. A signal made at close t first
owns price risk at open t+1; neutral means flat, and every change pays costs.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

from alpha_engine.cache.interface import Cache
from alpha_engine.cache.models import Candle, Fundamentals, Interval, NewsItem, PriceSeries
from alpha_engine.schema.signal import Direction, Market
from alpha_engine.strategy.metrics import bars_per_year_for, compute_metrics
from alpha_engine.validation.backtest import DEFAULT_WARMUP, signal_at
from alpha_engine.validation.compare_context import AAPL_HOLDOUT_START, read_news_archive


def _account(candles: list[Candle], targets: list[float], cost_bps: float) -> dict:
    """One target per close, executed at the following open; liquidate at final close."""
    fee = cost_bps / 10_000
    equity = [100_000.0]
    returns: list[float] = []
    previous = 0.0
    changes = 0
    active = 0
    for offset, target in enumerate(targets):
        if equity[-1] == 0:
            returns.append(0.0)
            equity.append(0.0)
            continue
        t = DEFAULT_WARMUP + offset
        entry = candles[t + 1].open
        exit_price = candles[t + 2].open if t + 2 < len(candles) else candles[-1].close
        if not all(math.isfinite(v) and v > 0 for v in (entry, exit_price)):
            raise ValueError("trade experiment needs positive finite open/close prices")
        turnover = abs(target - previous)
        changes += target != previous
        active += target != 0
        entry_factor = 1 - turnover * fee
        market_factor = 1 + target * (exit_price / entry - 1)
        growth = max(0.0, entry_factor) * max(0.0, market_factor)
        if offset == len(targets) - 1:
            growth *= max(0.0, 1 - abs(target) * fee)  # end-of-sample liquidation
            changes += target != 0
        net = growth - 1.0  # a short squeeze can wipe out, not invert, the account
        returns.append(net)
        equity.append(equity[-1] * (1 + net))
        previous = target
    metrics, _ = compute_metrics(equity, returns, [], bars_per_year=bars_per_year_for(Interval.DAY))
    return {
        "return_pct": metrics.total_return_pct,
        "sharpe": metrics.sharpe,
        "annual_volatility_pct": metrics.annual_volatility_pct,
        "max_drawdown_pct": metrics.max_drawdown_pct,
        "position_changes": changes,
        "active_intervals": active,
        "intervals": len(targets),
    }


def run_trade_experiment(
    series: PriceSeries,
    fundamentals: list[Fundamentals] | None = None,
    news: list[NewsItem] | None = None,
    *,
    cost_bps: float = 10.0,
) -> dict:
    """Compare frozen technical/context targets and benchmarks on one pre-holdout series."""
    if not math.isfinite(cost_bps) or cost_bps < 0:
        raise ValueError("cost_bps must be finite and nonnegative")
    if series.interval is not Interval.DAY:
        raise ValueError("trade experiment currently requires daily candles")
    candles = [c for c in series.candles if c.ts.date() < AAPL_HOLDOUT_START]
    if len(candles) < DEFAULT_WARMUP + 3:
        raise ValueError("not enough pre-holdout candles for next-open execution")
    if any(
        c.ts.tzinfo is None
        or not all(math.isfinite(v) and v > 0 for v in (c.open, c.high, c.low, c.close))
        or c.low > min(c.open, c.close)
        or c.high < max(c.open, c.close)
        for c in candles
    ) or any(a.ts >= b.ts for a, b in zip(candles, candles[1:])):
        raise ValueError("development candles need ordered unique timestamps and valid OHLC")
    past = series.model_copy(update={"candles": candles})
    # The live cache deduplicates URLs, but a research archive bypasses that cache.
    unique_news = {}
    for item in news or []:
        if series.asset.upper() in item.asset_tags:
            key = item.url or f"{item.source}:{item.ts.isoformat()}:{item.headline}"
            unique_news.setdefault(key, item)
    relevant_news = list(unique_news.values())
    modes = {"technical": {}, "technical_fundamental": {}}
    if relevant_news:
        modes["technical_fundamental_news"] = {}
    targets = {name: [] for name in modes}
    active = {"fundamentals": 0, "news": 0}
    for t in range(DEFAULT_WARMUP, len(candles) - 1):
        for name, kwargs in (
            ("technical", {}),
            ("technical_fundamental", {"fundamentals_data": fundamentals}),
            (
                "technical_fundamental_news",
                {"fundamentals_data": fundamentals, "news_data": relevant_news},
            ),
        ):
            if name not in modes:
                continue
            signal, _ = signal_at(past, t, market=Market.US_EQUITY, **kwargs)
            targets[name].append(
                1
                if signal.direction is Direction.BULLISH
                else -1
                if signal.direction is Direction.BEARISH
                else 0
            )
            if name == "technical_fundamental" and any(
                s.name == "fundamentals" and s.weight > 0 for s in signal.signal_sources
            ):
                active["fundamentals"] += 1
            if name == "technical_fundamental_news" and any(
                s.name == "news.sentiment" and s.weight > 0 for s in signal.signal_sources
            ):
                active["news"] += 1
    if not active["news"]:
        targets.pop("technical_fundamental_news", None)
    results = {name: _account(candles, target, cost_bps) for name, target in targets.items()}
    full_long = _account(candles, [1.0] * len(next(iter(targets.values()))), cost_bps)
    for name, result in results.items():
        # Ex-post volatility matching is a diagnostic only, never a trading rule.
        scale = (
            min(1.0, result["annual_volatility_pct"] / full_long["annual_volatility_pct"])
            if full_long["annual_volatility_pct"] > 0
            else 0.0
        )
        result["vol_matched_buy_hold_weight"] = round(scale, 4)
        result["vol_matched_buy_hold"] = _account(candles, [scale] * len(targets[name]), cost_bps)
    return {
        "asset": series.asset,
        "first_development_bar": candles[0].ts.isoformat(),
        "last_development_bar": candles[-1].ts.isoformat(),
        "reserved_holdout_bars": len(series.candles) - len(candles),
        "cost_bps_per_side": cost_bps,
        "fundamentals_active_closes": active["fundamentals"],
        "news_active_closes": active["news"],
        "buy_hold": full_long,
        "modes": results,
        "warning": (
            "RESEARCH ONLY. Not financial advice. Signals fill at next open; fees are flat, "
            "slippage/borrow/taxes are omitted. Volatility matching uses full-sample realized "
            "volatility (hindsight) and is diagnostic, not executable. In-sample, no alpha claim."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Next-open equity signal trade experiment")
    parser.add_argument("assets", nargs="*", default=["AAPL", "MSFT", "GOOGL", "NVDA"])
    parser.add_argument("--cost-bps", type=float, default=10.0)
    parser.add_argument("--news-file", type=Path, help="dated research news archive for one asset")
    args = parser.parse_args()
    if args.news_file and len(args.assets) != 1:
        parser.error("--news-file requires exactly one asset")
    cache = Cache()
    news, _ = cache.get_news()
    reports = {}
    for asset in args.assets:
        asset = asset.upper()
        series, _ = cache.get_price(asset, "1d")
        if series is None:
            parser.error(f"no cached daily candles for {asset}")
        fundamentals, _ = cache.get_fundamentals(asset)
        try:
            archive_info = None
            asset_news = news
            if args.news_file:
                development = [c for c in series.candles if c.ts.date() < AAPL_HOLDOUT_START]
                asset_news, archive_info = read_news_archive(
                    args.news_file,
                    asset,
                    development[DEFAULT_WARMUP].ts.date(),
                    development[-1].ts.date(),
                )
            reports[asset] = run_trade_experiment(
                series, fundamentals, asset_news, cost_bps=args.cost_bps
            )
            if archive_info is not None:
                reports[asset]["news_archive"] = archive_info
        except (IndexError, ValueError) as exc:
            parser.error(f"{asset}: {exc}")
    print(json.dumps(reports, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
