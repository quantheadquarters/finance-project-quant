"""Frozen, research-only US-equity momentum candidate; never a live signal.

At each month's first trading close, hold up to two names with positive 12-to-2
month momentum and a price above its 200-day average. A public filing showing
negative year-over-year quarterly revenue vetoes a name; missing data does not.
All targets fill at the next open, with per-side costs and final liquidation.
"""

from __future__ import annotations

import argparse
import json
import math
from datetime import datetime

from alpha_engine.cache.interface import Cache
from alpha_engine.cache.models import Candle, Fundamentals, Interval, PriceSeries
from alpha_engine.strategy.metrics import bars_per_year_for, compute_metrics
from alpha_engine.validation.compare_context import AAPL_HOLDOUT_START

ASSETS = ("AAPL", "MSFT", "GOOGL", "NVDA")
WARMUP = 252
TOP = 2


def _revenue_veto(rows: list[Fundamentals], asof: datetime) -> bool:
    """Only a public, sufficiently fresh same-quarter revenue decline can veto."""
    public = [r for r in rows if r.available_at is not None and r.available_at <= asof]
    if not public:
        return False
    latest = max(public, key=lambda r: r.ts)
    if (asof - latest.available_at).days > 180 or latest.revenue is None:
        return False
    try:
        year, quarter = latest.period.split("-", 1)
        prior_period = f"{int(year) - 1}-{quarter}"
    except ValueError:
        return False
    prior = next((r for r in public if r.period == prior_period), None)
    return bool(prior and prior.revenue and latest.revenue < prior.revenue)


def _account(
    bars: dict[str, list[Candle]], targets: list[dict[str, float]], cost_bps: float
) -> dict:
    fee = cost_bps / 10_000
    weights = {asset: 0.0 for asset in bars}
    previous_target: dict[str, float] | None = None
    equity = [100_000.0]
    returns = []
    annual_growth: dict[int, float] = {}
    turnover = 0.0
    for offset, target in enumerate(targets):
        t = WARMUP + offset
        changes = 0.0
        if target != previous_target:
            changes = sum(abs(target[a] - weights[a]) for a in bars)
            turnover += changes
            weights = target.copy()
        asset_returns = {
            a: (bars[a][t + 2].open if t + 2 < len(bars[a]) else bars[a][-1].close)
            / bars[a][t + 1].open
            - 1
            for a in bars
        }
        market_growth = 1 + sum(weights[a] * asset_returns[a] for a in bars)
        growth = (1 - changes * fee) * market_growth
        weights = {a: weights[a] * (1 + asset_returns[a]) / market_growth for a in bars}
        if offset == len(targets) - 1:
            turnover += sum(weights.values())
            growth *= 1 - sum(weights.values()) * fee
        growth = max(0.0, growth)
        returns.append(growth - 1)
        equity.append(equity[-1] * growth)
        year = bars[ASSETS[0]][t + 1].ts.year
        annual_growth[year] = annual_growth.get(year, 1.0) * growth
        previous_target = target
    metrics, _ = compute_metrics(equity, returns, [], bars_per_year=bars_per_year_for(Interval.DAY))
    return {
        "return_pct": round(metrics.total_return_pct, 3),
        "sharpe": round(metrics.sharpe, 3),
        "max_drawdown_pct": round(metrics.max_drawdown_pct, 3),
        "turnover": round(turnover, 3),
        "calendar_return_pct": {
            str(year): round((value - 1) * 100, 3) for year, value in annual_growth.items()
        },
    }


def evaluate(
    series: dict[str, PriceSeries],
    fundamentals: dict[str, list[Fundamentals]],
    *,
    cost_bps: float = 10.0,
) -> dict:
    """Evaluate predeclared rules only before the sealed AAPL holdout date."""
    if not math.isfinite(cost_bps) or cost_bps < 0:
        raise ValueError("cost_bps must be finite and nonnegative")
    if set(series) != set(ASSETS) or any(s.interval is not Interval.DAY for s in series.values()):
        raise ValueError("candidate requires the four declared daily series")
    common = sorted(
        d
        for d in set.intersection(*(set(c.ts.date() for c in s.candles) for s in series.values()))
        if d < AAPL_HOLDOUT_START
    )
    if len(common) < WARMUP + 3:
        raise ValueError("insufficient common pre-holdout trading days")
    bars = {
        a: [lookup[d] for d in common]
        for a, s in series.items()
        for lookup in [{c.ts.date(): c for c in s.candles}]
    }
    if any(
        len({c.ts.date() for c in s.candles}) != len(s.candles)
        or any(x.ts >= y.ts for x, y in zip(s.candles, s.candles[1:]))
        for s in series.values()
    ) or any(
        not all(math.isfinite(v) and v > 0 for v in (c.open, c.close))
        for candles in bars.values()
        for c in candles
    ):
        raise ValueError("candles must be ordered, unique, and have positive finite prices")

    technical: list[dict[str, float]] = []
    confirmed: list[dict[str, float]] = []
    benchmark: list[dict[str, float]] = []
    months = 0
    vetoes = 0
    picks = {"technical": {a: 0 for a in ASSETS}, "confirmed": {a: 0 for a in ASSETS}}
    for t in range(WARMUP, len(common) - 1):
        month = (common[t].year, common[t].month)
        if t == WARMUP or month != (common[t - 1].year, common[t - 1].month):
            months += 1
            ranked = sorted(
                (
                    (bars[a][t - 21].close / bars[a][t - 252].close - 1, a)
                    for a in ASSETS
                    if bars[a][t].close > sum(c.close for c in bars[a][t - 199 : t + 1]) / 200
                ),
                reverse=True,
            )
            choices = [a for score, a in ranked if score > 0][:TOP]
            approved = [
                a
                for score, a in ranked
                if score > 0 and not _revenue_veto(fundamentals.get(a, []), bars[a][t].ts)
            ][:TOP]
            vetoes += sum(a not in approved for a in choices)
            technical_weights = {a: (1 / TOP if a in choices else 0.0) for a in ASSETS}
            confirmed_weights = {a: (1 / TOP if a in approved else 0.0) for a in ASSETS}
            for a in choices:
                picks["technical"][a] += 1
            for a in approved:
                picks["confirmed"][a] += 1
        technical.append(technical_weights)
        confirmed.append(confirmed_weights)
        benchmark.append({a: 1 / len(ASSETS) for a in ASSETS})
    return {
        "rule": "monthly top-2 positive 12-to-2-month momentum above 200-day SMA; negative filed YoY revenue veto",
        "assets": list(ASSETS),
        "start": common[WARMUP].isoformat(),
        "end": common[-1].isoformat(),
        "holdout_start": AAPL_HOLDOUT_START.isoformat(),
        "reserved_holdout_bars": sum(
            c.ts.date() >= AAPL_HOLDOUT_START for c in series["AAPL"].candles
        ),
        "common_days": len(common),
        "rebalance_months": months,
        "revenue_vetoed_initial_picks": vetoes,
        "picks": picks,
        "cost_bps_per_side": cost_bps,
        "technical": _account(bars, technical, cost_bps),
        "technical_fundamental": _account(bars, confirmed, cost_bps),
        "equal_weight_buy_hold": _account(bars, benchmark, cost_bps),
        "warning": (
            "RESEARCH ONLY. Not financial advice. In-sample, survivor-selected four-stock universe; "
            "not demonstrated alpha. Next-open fills with flat fees; dividends, slippage, taxes, "
            "market impact, and delistings omitted. Do not trade from this report."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cost-bps", type=float, default=10.0)
    args = parser.parse_args()
    cache = Cache()
    series = {a: cache.get_price(a, "1d")[0] for a in ASSETS}
    if any(s is None for s in series.values()):
        parser.error("first cache daily prices for AAPL MSFT GOOGL NVDA")
    report = evaluate(
        series, {a: cache.get_fundamentals(a)[0] for a in ASSETS}, cost_bps=args.cost_bps
    )
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
