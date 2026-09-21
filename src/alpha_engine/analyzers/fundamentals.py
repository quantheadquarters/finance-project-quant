"""Fundamentals analyzer: earnings quality and balance-sheet strength.

Three deterministic reads, each computable from reported figures alone:

1. **Earnings quality (accruals).** Net income is an opinion; cash flow is a
   fact. When a company reports rising profits while operating cash flow lags,
   the gap is accruals — revenue booked but not yet collected. Persistently
   high accruals are the single best-documented red flag in the accounting
   literature. Cash flow comfortably above net income votes bullish.

2. **Balance-sheet strength (leverage).** Debt-to-equity, judged against fixed
   thresholds. Not a prediction — a fragility measure. A highly levered company
   is not doomed, it just has fewer options when something goes wrong.

3. **Growth.** Year-over-year revenue change across available periods.

**What this analyzer deliberately refuses to do:** value the company. DCF needs
a discount rate and a terminal growth rate; comparables need a peer set. Both
are judgment calls, and a number you invented does not stop being invented
because you put it in a spreadsheet. FUTURE_WORK defers them explicitly and this
file honours that.

Valuation percentile against the company's *own* history is the one valuation
read that needs no assumptions — but it needs a price series, so it lives with
the caller rather than here.

Every ratio abstains when its inputs are missing. Free fundamentals data is
patchy, and a missing margin must never become a zero margin.
"""

from __future__ import annotations

import math

from alpha_engine.cache.models import Fundamentals
from alpha_engine.schema.signal import Direction, SignalSource

# Fundamentals move slowly and this engine trades swing horizons. Real, but
# never the loudest voice in the room.
MAX_WEIGHT = 0.35

# Accruals: operating cash flow relative to net income.
ACCRUAL_HEALTHY = 1.10  # cash flow 10% above reported profit = clean earnings
ACCRUAL_SUSPECT = 0.80  # cash flow 20% below profit = the gap is accruals

# Leverage: total debt over total equity.
LEVERAGE_LOW = 0.5
LEVERAGE_HIGH = 2.0

# Revenue growth, year over year.
GROWTH_STRONG = 0.15
GROWTH_CONTRACTING = -0.05

_DEADBAND = 0.15


def _ordered(periods: list[Fundamentals]) -> list[Fundamentals]:
    return sorted(periods, key=lambda f: f.ts)


def accrual_ratio(period: Fundamentals) -> float | None:
    """Operating cash flow over net income. None when either is missing, or
    when net income is non-positive — the ratio is not meaningful for a
    loss-making quarter and a negative denominator would invert its sense."""
    if period.operating_cash_flow is None or period.net_income is None:
        return None
    if period.net_income <= 0:
        return None
    return period.operating_cash_flow / period.net_income


def leverage_ratio(period: Fundamentals) -> float | None:
    """Total debt over total equity. None when equity is missing or negative;
    negative equity is a distress signal in its own right, not a ratio."""
    if period.total_debt is None or period.total_equity is None:
        return None
    if period.total_equity <= 0:
        return None
    return period.total_debt / period.total_equity


def revenue_growth(periods: list[Fundamentals]) -> float | None:
    """Year-over-year growth only when the matching quarter is actually present.

    Counting four rows back lies when a source skips Q4; comparing adjacent
    quarters is not year-over-year growth either. Missing comparison abstains.
    """
    ordered = [p for p in _ordered(periods) if p.revenue is not None and p.revenue > 0]
    if len(ordered) < 2:
        return None
    latest = ordered[-1]
    try:
        year, quarter = latest.period.split("-")
        previous_period = f"{int(year) - 1}-{quarter}"
    except ValueError:
        return None
    base = next((p for p in reversed(ordered[:-1]) if p.period == previous_period), None)
    if base is None:
        return None
    return latest.revenue / base.revenue - 1.0


def analyze_fundamentals(periods: list[Fundamentals]) -> SignalSource:
    """Fold available fundamentals into one SignalSource."""
    if not periods:
        return SignalSource(
            name="fundamentals",
            direction=Direction.NEUTRAL,
            weight=0.0,
            detail="no fundamentals data",
        )

    ordered = _ordered(periods)
    latest = ordered[-1]

    votes: list[float] = []
    notes: list[str] = []

    accruals = accrual_ratio(latest)
    if accruals is not None:
        if accruals >= ACCRUAL_HEALTHY:
            votes.append(1.0)
        elif accruals <= ACCRUAL_SUSPECT:
            votes.append(-1.0)
        else:
            votes.append(0.0)
        notes.append(f"ocf/ni={accruals:.2f}")

    leverage = leverage_ratio(latest)
    if leverage is not None:
        if leverage <= LEVERAGE_LOW:
            votes.append(1.0)
        elif leverage >= LEVERAGE_HIGH:
            votes.append(-1.0)
        else:
            votes.append(0.0)
        notes.append(f"d/e={leverage:.2f}")

    growth = revenue_growth(ordered)
    if growth is not None:
        if growth >= GROWTH_STRONG:
            votes.append(1.0)
        elif growth <= GROWTH_CONTRACTING:
            votes.append(-1.0)
        else:
            votes.append(0.0)
        notes.append(f"rev_growth={growth:+.1%}")

    if not votes:
        return SignalSource(
            name="fundamentals",
            direction=Direction.NEUTRAL,
            weight=0.0,
            detail=f"{len(periods)} periods but no computable ratios",
        )

    score = sum(votes) / len(votes)

    if score > _DEADBAND:
        direction = Direction.BULLISH
    elif score < -_DEADBAND:
        direction = Direction.BEARISH
    else:
        direction = Direction.NEUTRAL

    # One ratio is a data point; three agreeing is a picture.
    breadth = min(1.0, math.sqrt(len(votes)) / math.sqrt(3.0))
    weight = round(min(abs(score), 1.0) * breadth * MAX_WEIGHT, 4)

    return SignalSource(
        name="fundamentals",
        direction=direction,
        weight=weight,
        detail=" ".join(notes) + f" score={score:+.2f}",
    )
