"""Outcome scoring: did the market do what the signal said, before the thesis
was invalidated?

The rules are deliberately strict and symmetric, because loose scoring is how
track records quietly become dishonest:

- A signal is judged only over its own horizon (bars per timeframe, below).
  A bullish call that pays off a year later was still a miss on its terms.
- Touching the invalidation level ends the trade as a miss immediately, even if
  price later recovers. The invalidation level means what it says.
- Neutral signals and records without an entry price are not scorable; they are
  counted separately, never silently dropped.
- Realized return is signed in the direction of the call, so a number > 0 always
  means "the call captured a move" for bullish and bearish alike.

Everything here is a pure function of its inputs: no network, no clock reads.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import Enum

from pydantic import BaseModel, Field

from alpha_engine.cache.models import Candle, PriceSeries
from alpha_engine.schema.signal import Direction, Timeframe
from alpha_engine.validation.recorder import SignalRecord

# Evaluation horizon per timeframe, in daily bars. A swing view gets ~two weeks
# to play out; a position view gets ~six. Tune deliberately: changing these
# changes every historical score.
HORIZON_BARS: dict[Timeframe, int] = {
    Timeframe.INTRADAY: 1,
    Timeframe.SWING: 10,
    Timeframe.POSITION: 30,
}


class OutcomeStatus(str, Enum):
    RESOLVED = "resolved"  # enough later data existed to judge the call
    PENDING = "pending"  # too early to judge; not enough bars after emission
    NOT_APPLICABLE = "not_applicable"  # neutral direction or no entry price


class Outcome(BaseModel):
    """The realized result of one signal. `hit` and `realized_return` are None
    unless status is RESOLVED."""

    status: OutcomeStatus
    hit: bool | None = None
    realized_return: float | None = Field(
        None, description="signed in the call's direction; >0 means move captured"
    )
    invalidated: bool = False
    bars_evaluated: int = 0


def score_forward(
    direction: Direction,
    entry_price: float,
    invalidation_level: float | None,
    future: list[Candle],
    horizon: int,
) -> Outcome:
    """Score a directional call against the candles that came after it.

    `future` must contain only bars strictly after the signal; callers own that
    guarantee (see `score_record` and the backtester's slicing).
    """
    window = future[:horizon]

    for i, candle in enumerate(window):
        if invalidation_level is None:
            break
        breached = (
            candle.low <= invalidation_level
            if direction is Direction.BULLISH
            else candle.high >= invalidation_level
        )
        if breached:
            # Conservative accounting: assume the exit happened at the
            # invalidation level, not at the (unknowable) intrabar price.
            raw = (invalidation_level - entry_price) / entry_price
            realized = raw if direction is Direction.BULLISH else -raw
            return Outcome(
                status=OutcomeStatus.RESOLVED,
                hit=False,
                realized_return=round(realized, 6),
                invalidated=True,
                bars_evaluated=i + 1,
            )

    if len(window) < horizon:
        return Outcome(
            status=OutcomeStatus.PENDING,
            realized_return=None,
            bars_evaluated=len(window),
        )

    exit_price = window[-1].close
    raw = (exit_price - entry_price) / entry_price
    realized = raw if direction is Direction.BULLISH else -raw
    return Outcome(
        status=OutcomeStatus.RESOLVED,
        hit=realized > 0,
        realized_return=round(realized, 6),
        bars_evaluated=horizon,
    )


def score_record(record: SignalRecord, series: PriceSeries) -> Outcome:
    """Score a recorded live signal against a (possibly fresher) price series.

    Only candles strictly after the signal's timestamp count as "future" — the
    same no-lookahead discipline the backtester enforces by index.
    """
    signal = record.signal
    if signal.direction is Direction.NEUTRAL or not record.entry_price:
        return Outcome(
            status=OutcomeStatus.NOT_APPLICABLE,
            realized_return=None,
        )

    future = [c for c in series.candles if c.ts > signal.timestamp]
    return score_forward(
        direction=signal.direction,
        entry_price=record.entry_price,
        invalidation_level=signal.invalidation_level,
        future=future,
        horizon=HORIZON_BARS[signal.timeframe],
    )


class CalibrationBin(BaseModel):
    """One row of the calibration curve: of the resolved signals whose stated
    confidence fell in [lo, hi), how many were actually right? A calibrated
    engine shows hit_rate tracking the bin midpoint; the scaffold won't, yet."""

    lo: float
    hi: float
    count: int
    hits: int
    hit_rate: float | None


class OutcomeSummary(BaseModel):
    """Aggregate honesty report over a set of scored signals."""

    total: int
    resolved: int
    pending: int
    not_applicable: int
    hits: int
    hit_rate: float | None = Field(None, description="hits / resolved; None if nothing resolved")
    avg_realized_return: float | None = None
    calibration: list[CalibrationBin] = Field(default_factory=list)


def summarize_outcomes(scored: list[tuple[float, Outcome]]) -> OutcomeSummary:
    """Fold (confidence, outcome) pairs into the summary + calibration curve."""
    resolved = [(conf, o) for conf, o in scored if o.status is OutcomeStatus.RESOLVED]
    pending = sum(1 for _, o in scored if o.status is OutcomeStatus.PENDING)
    n_a = sum(1 for _, o in scored if o.status is OutcomeStatus.NOT_APPLICABLE)

    hits = sum(1 for _, o in resolved if o.hit)
    returns = [o.realized_return for _, o in resolved if o.realized_return is not None]

    bins: list[CalibrationBin] = []
    edges = [0.0, 0.2, 0.4, 0.6, 0.8, 1.0]
    for lo, hi in zip(edges[:-1], edges[1:]):
        in_bin = [o for conf, o in resolved if lo <= conf < hi or (hi == 1.0 and conf == 1.0)]
        bin_hits = sum(1 for o in in_bin if o.hit)
        bins.append(
            CalibrationBin(
                lo=lo,
                hi=hi,
                count=len(in_bin),
                hits=bin_hits,
                hit_rate=round(bin_hits / len(in_bin), 4) if in_bin else None,
            )
        )

    return OutcomeSummary(
        total=len(scored),
        resolved=len(resolved),
        pending=pending,
        not_applicable=n_a,
        hits=hits,
        hit_rate=round(hits / len(resolved), 4) if resolved else None,
        avg_realized_return=round(sum(returns) / len(returns), 6) if returns else None,
        calibration=bins,
    )


# Below this fraction of scored records, the track record withholds its hit rate
# instead of reporting one computed over an asset-biased subset.
MIN_SCORED_FRACTION = 0.95


def annotate_coverage(
    payload: dict,
    *,
    total: int,
    scored: int,
    missing_assets: Iterable[str],
    min_fraction: float = MIN_SCORED_FRACTION,
) -> dict:
    """Add scoring-coverage fields to a summary payload, and withhold `hit_rate`
    when too little of the record could be scored.

    A record is skipped when its asset is absent from the local, regenerable
    price cache. That exclusion is not random: it drops whole assets, and which
    ones depends on the machine. A hit rate over the remainder is a different
    statistic wearing the same name — on 2026-08-31 this reported 0.1184 where
    the full sample gave 0.3050, with the average return flipped in sign.

    Callers that render to stderr can still print their own note; this exists so
    the *payload* carries the caveat, since stderr does not survive a pipe into a
    dashboard or an MCP client.

    Mutates and returns `payload`.
    """
    missing = sorted(set(missing_assets))
    skipped = total - scored
    fraction = scored / total if total else 0.0

    payload["records_total"] = total
    payload["records_scored"] = scored
    payload["records_skipped"] = skipped
    payload["scored_fraction"] = round(fraction, 4)
    payload["skipped_assets"] = missing

    if skipped and fraction < min_fraction:
        payload["hit_rate"] = None
        payload["hit_rate_suppressed"] = (
            f"only {scored} of {total} records could be scored ({fraction:.1%}); "
            f"the missing assets are {missing}. Backfill their price cache and "
            "re-run — a hit rate over this subset would describe a different, "
            "machine-dependent sample."
        )
    return payload
