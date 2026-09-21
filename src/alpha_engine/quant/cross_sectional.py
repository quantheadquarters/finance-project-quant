"""Research-only cross-sectional factor model.

At each date the model ranks stocks against each other, then holds the strongest
and weakest names for ``horizon`` bars.  Fitting is expanding-window: a target is
not admitted to training until its whole forward window is in the past.  That is
the no-lookahead rule in executable form.

This module deliberately does not emit a ``SignalSource``.  The repository does
not yet have enough untouched, point-in-time history to put a learned model in
the live decision path.  It is a candidate evaluator, not evidence of alpha.

Run it against daily series already in the cache::

    python -m alpha_engine.quant.cross_sectional AAPL MSFT GOOGL NVDA
"""

from __future__ import annotations

import argparse
import json
import math
import random
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from statistics import mean, pstdev

from alpha_engine.cache.interface import Cache
from alpha_engine.cache.models import PriceSeries
from alpha_engine.quant.factors import compute_panel
from alpha_engine.quant.ranking import _spearman_rank

DEFAULT_FACTORS = (
    "mom_20",
    "mom_60",
    "dist_sma_20",
    "rvol_20",
    "range_position_20",
    "volume_z_20",
)


@dataclass(frozen=True)
class PeriodResult:
    """One prediction made before its forward return was known."""

    prediction_date: str
    long: list[str]
    short: list[str]
    gross_return: float
    turnover: float
    net_return: float
    rank_ic: float | None


@dataclass(frozen=True)
class CrossSectionalReport:
    """Measured candidate performance; never a live trading signal."""

    assets: list[str]
    factors: list[str]
    horizon: int
    cost_bps: float
    ridge: float
    common_dates: int
    periods: list[PeriodResult]
    total_return: float
    annualized_sharpe: float
    win_rate: float
    mean_rank_ic: float | None
    shuffled_total_return: float
    verdict: str
    disclaimer: str = "RESEARCH ONLY. Not financial advice."


def _solve_ridge(x: list[list[float]], y: list[float], penalty: float) -> list[float]:
    """Solve ridge regression's normal equations with stdlib Gaussian elimination."""
    width = len(x[0])
    matrix = [
        [sum(row[i] * row[j] for row in x) + (penalty if i == j else 0.0) for j in range(width)]
        + [sum(row[i] * target for row, target in zip(x, y))]
        for i in range(width)
    ]

    for col in range(width):
        pivot = max(range(col, width), key=lambda row: abs(matrix[row][col]))
        matrix[col], matrix[pivot] = matrix[pivot], matrix[col]
        divisor = matrix[col][col]
        if abs(divisor) < 1e-12:
            continue
        matrix[col] = [value / divisor for value in matrix[col]]
        for row in range(width):
            if row == col:
                continue
            scale = matrix[row][col]
            matrix[row] = [a - scale * b for a, b in zip(matrix[row], matrix[col])]
    return [matrix[i][-1] for i in range(width)]


def _zscore_rows(rows: list[list[float]]) -> list[list[float]]:
    """Standardize each feature across the stocks visible on one date."""
    width = len(rows[0])
    columns = [[row[i] for row in rows] for i in range(width)]
    centers = [mean(column) for column in columns]
    scales = [pstdev(column) or 1.0 for column in columns]
    return [[(value - centers[i]) / scales[i] for i, value in enumerate(row)] for row in rows]


def _rank_ic(predicted: list[float], actual: list[float]) -> float | None:
    if len(predicted) < 3:
        return None
    x, y = _spearman_rank(predicted), _spearman_rank(actual)
    mx, my = mean(x), mean(y)
    numerator = sum((a - mx) * (b - my) for a, b in zip(x, y))
    denominator = math.sqrt(sum((a - mx) ** 2 for a in x) * sum((b - my) ** 2 for b in y))
    return numerator / denominator if denominator else None


def _weights(
    assets: list[str], scores: list[float]
) -> tuple[dict[str, float], list[str], list[str]]:
    count = max(1, len(assets) // 4)
    ranked = sorted(zip(scores, assets))
    short = [asset for _, asset in ranked[:count]]
    long = [asset for _, asset in ranked[-count:]]
    weights = {asset: 0.0 for asset in assets}
    for asset in long:
        weights[asset] = 1.0 / count
    for asset in short:
        weights[asset] = -1.0 / count
    return weights, long, short


def _portfolio_return(
    weights: dict[str, float],
    previous: dict[str, float],
    returns: dict[str, float],
    cost_bps: float,
) -> tuple[float, float, float]:
    gross = sum(weight * returns[asset] for asset, weight in weights.items())
    turnover = sum(abs(weight - previous.get(asset, 0.0)) for asset, weight in weights.items())
    return gross, turnover, gross - turnover * cost_bps / 10_000.0


def evaluate_cross_sectional(
    series_by_asset: dict[str, PriceSeries],
    factors: Sequence[str] = DEFAULT_FACTORS,
    *,
    horizon: int = 10,
    min_train_dates: int = 126,
    cost_bps: float = 10.0,
    ridge: float = 1.0,
    null_runs: int = 20,
    seed: int = 0,
) -> CrossSectionalReport:
    """Walk forward through cached series without using future information to fit."""
    if len(series_by_asset) < 4:
        raise ValueError("cross-sectional research needs at least 4 assets")
    if horizon < 1 or min_train_dates < 10 or cost_bps < 0 or ridge <= 0 or null_runs < 1:
        raise ValueError("invalid horizon, training window, cost, ridge, or null run count")

    assets = sorted(series_by_asset)
    requested = list(factors)
    if not requested:
        raise ValueError("at least one factor is required")

    by_day = {
        asset: {c.ts.date(): (i, c.close) for i, c in enumerate(series_by_asset[asset].candles)}
        for asset in assets
    }
    common_days = sorted(set.intersection(*(set(days) for days in by_day.values())))
    if len(common_days) < min_train_dates + 2 * horizon:
        raise ValueError("not enough common history for the requested training window and horizon")

    panels = {asset: compute_panel(series_by_asset[asset], names=requested) for asset in assets}
    features: dict[int, list[list[float]]] = {}
    forward: dict[int, dict[str, float]] = {}
    for i, day in enumerate(common_days):
        rows: list[list[float]] = []
        for asset in assets:
            original_index, _ = by_day[asset][day]
            row = [panels[asset][name][original_index] for name in requested]
            if any(value is None or not math.isfinite(value) for value in row):
                break
            rows.append([float(value) for value in row])
        if len(rows) == len(assets):
            features[i] = _zscore_rows(rows)
        if i + horizon < len(common_days):
            end = common_days[i + horizon]
            raw = {asset: by_day[asset][end][1] / by_day[asset][day][1] - 1.0 for asset in assets}
            center = mean(raw.values())
            forward[i] = {asset: value - center for asset, value in raw.items()}

    eligible = [i for i in sorted(features) if i in forward]
    evaluation: list[int] = []
    last = -horizon
    for i in eligible:
        known = [j for j in eligible if j + horizon <= i]
        if len(known) >= min_train_dates and i - last >= horizon:
            evaluation.append(i)
            last = i
    if not evaluation:
        raise ValueError("no walk-forward periods remain after factor warmup")

    rng = random.Random(seed)
    previous: dict[str, float] = {}
    null_previous = [{} for _ in range(null_runs)]
    null_returns = [[] for _ in range(null_runs)]
    periods: list[PeriodResult] = []

    for i in evaluation:
        train_dates = [j for j in eligible if j + horizon <= i]
        x = [row for j in train_dates for row in features[j]]
        y = [forward[j][asset] for j in train_dates for asset in assets]
        coefficients = _solve_ridge(x, y, ridge)
        scores = [sum(a * b for a, b in zip(row, coefficients)) for row in features[i]]
        weights, long, short = _weights(assets, scores)
        gross, turnover, net = _portfolio_return(weights, previous, forward[i], cost_bps)
        actual = [forward[i][asset] for asset in assets]
        periods.append(
            PeriodResult(
                prediction_date=common_days[i].isoformat(),
                long=long,
                short=short,
                gross_return=round(gross, 8),
                turnover=round(turnover, 4),
                net_return=round(net, 8),
                rank_ic=_rank_ic(scores, actual),
            )
        )
        previous = weights

        for run in range(null_runs):
            shuffled = scores[:]
            rng.shuffle(shuffled)
            null_weights, _, _ = _weights(assets, shuffled)
            _, _, null_net = _portfolio_return(
                null_weights, null_previous[run], forward[i], cost_bps
            )
            null_returns[run].append(null_net)
            null_previous[run] = null_weights

    returns = [period.net_return for period in periods]
    total = math.prod(1.0 + value for value in returns) - 1.0
    volatility = pstdev(returns) if len(returns) > 1 else 0.0
    sharpe = mean(returns) / volatility * math.sqrt(252.0 / horizon) if volatility else 0.0
    ics = [period.rank_ic for period in periods if period.rank_ic is not None]
    null_totals = [math.prod(1.0 + value for value in run) - 1.0 for run in null_returns]
    shuffled_total = mean(null_totals)
    verdict = (
        "candidate beat this shuffled baseline; still not alpha without an untouched holdout"
        if total > shuffled_total
        else "no alpha detected: candidate did not beat the shuffled baseline"
    )

    return CrossSectionalReport(
        assets=assets,
        factors=requested,
        horizon=horizon,
        cost_bps=cost_bps,
        ridge=ridge,
        common_dates=len(common_days),
        periods=periods,
        total_return=round(total, 6),
        annualized_sharpe=round(sharpe, 4),
        win_rate=round(sum(value > 0 for value in returns) / len(returns), 4),
        mean_rank_ic=round(mean(ics), 4) if ics else None,
        shuffled_total_return=round(shuffled_total, 6),
        verdict=verdict,
    )


def _load_cached(assets: list[str]) -> dict[str, PriceSeries]:
    cache = Cache()
    loaded: dict[str, PriceSeries] = {}
    for asset in assets:
        series, _ = cache.get_price(asset, "1d")
        if series is not None:
            loaded[asset.upper()] = series
    return loaded


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Walk-forward cross-sectional model research")
    parser.add_argument("assets", nargs="+", help="cached daily equity symbols")
    parser.add_argument("--horizon", type=int, default=10)
    parser.add_argument("--min-train", type=int, default=126)
    parser.add_argument("--cost-bps", type=float, default=10.0)
    parser.add_argument("--ridge", type=float, default=1.0)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    loaded = _load_cached(args.assets)
    missing = sorted(set(map(str.upper, args.assets)) - set(loaded))
    if missing:
        parser.error(f"no cached daily prices for: {', '.join(missing)}")
    try:
        report = evaluate_cross_sectional(
            loaded,
            horizon=args.horizon,
            min_train_dates=args.min_train,
            cost_bps=args.cost_bps,
            ridge=args.ridge,
        )
    except ValueError as exc:
        parser.error(str(exc))

    payload = asdict(report)
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"Assets: {', '.join(report.assets)}")
        print(f"Walk-forward periods: {len(report.periods)}")
        print(f"Net return: {report.total_return:+.2%}")
        print(f"Annualized Sharpe: {report.annualized_sharpe:+.2f}")
        print(f"Mean rank IC: {report.mean_rank_ic}")
        print(f"Shuffled-signal return: {report.shuffled_total_return:+.2%}")
        print(f"Verdict: {report.verdict}")
        print(report.disclaimer)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
