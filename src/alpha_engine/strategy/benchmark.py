"""Pure, date-aligned comparisons for a trade-level strategy backtest."""

from __future__ import annotations

import math
from statistics import pstdev

from alpha_engine.cache.models import Candle, Interval
from alpha_engine.strategy.metrics import bars_per_year_for, drawdown_series


def compare_with_buy_hold(
    candles: list[Candle],
    strategy_equity: list[float],
    interval: Interval,
    capital: float,
    txn_cost_bps: float,
) -> dict:
    """Enter at bar 1 open, matching when a close-of-bar rule can first trade.

    The risk comparison mixes buy-and-hold daily returns with cash at a fixed
    fraction selected *after* seeing strategy volatility. It is a diagnostic,
    not a tradeable historical portfolio or evidence of excess return.
    """
    if len(candles) != len(strategy_equity) or len(candles) < 2:
        raise ValueError("comparison needs aligned strategy equity and at least two candles")
    fee = txn_cost_bps / 10_000.0
    equity = [capital]
    for i in range(1, len(candles)):
        previous_close = candles[i - 1].close
        today = candles[i]
        if not all(math.isfinite(v) and v > 0 for v in (previous_close, today.open, today.close)):
            raise ValueError("comparison needs positive finite prices")
        ratio = today.close / (today.open if i == 1 else previous_close)
        equity.append(equity[-1] * ratio * (1 - fee if i == 1 else 1))

    base_returns = [
        equity[i] / equity[i - 1] - 1 if equity[i - 1] > 0 else 0.0 for i in range(1, len(equity))
    ]
    strategy_returns = [
        strategy_equity[i] / strategy_equity[i - 1] - 1 if strategy_equity[i - 1] > 0 else 0.0
        for i in range(1, len(strategy_equity))
    ]
    annualizer = math.sqrt(bars_per_year_for(interval))
    base_vol = pstdev(base_returns) * annualizer if len(base_returns) > 1 else 0.0
    strategy_vol = pstdev(strategy_returns) * annualizer if len(strategy_returns) > 1 else 0.0
    fraction = min(1.0, strategy_vol / base_vol) if base_vol > 0 else 0.0
    risk_equity = [capital]
    for value in base_returns:
        risk_equity.append(risk_equity[-1] * (1 + fraction * value))

    def summary(values: list[float]) -> dict:
        return {
            "total_return_pct": round((values[-1] / values[0] - 1) * 100, 2),
            "max_drawdown_pct": round(min(drawdown_series(values)) * 100, 2),
            "equity_curve": [round(value, 2) for value in values],
        }

    return {
        "buy_and_hold": summary(equity),
        "risk_matched": {
            **summary(risk_equity),
            "stock_fraction": round(fraction, 4),
            "target_annual_volatility_pct": round(strategy_vol * 100, 2),
            "achieved_annual_volatility_pct": round(base_vol * fraction * 100, 2),
            "note": "Hindsight comparison: cash fraction uses full-period realized volatility; "
            "rebalancing and borrowing costs are not modeled. Do not treat it as a "
            "tradeable strategy. Exact matching is unavailable when strategy volatility "
            "exceeds unlevered buy-and-hold.",
        },
        "method": "Same bars and starting capital; buy-and-hold enters bar 1 open and "
        "pays one entry fee. Cash earns zero. Historical prices may omit corporate actions.",
    }
