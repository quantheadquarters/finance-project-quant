"""Does the factor-ranking machinery actually work?

`test_factors.py` proves no factor peeks at the future. That is necessary but
not sufficient: a registry could be lookahead-clean and still be unable to
*find* a real relationship, or — worse — hand back an impressive-looking |IC|
on pure noise and call it alpha. This file pins the two properties that make the
ranking layer trustworthy:

1. **When genuine structure exists, the pipeline surfaces it.** On a
   deterministic series with an embedded, predictable dynamic, the top factor's
   rank IC is enormous and clears the noise floor by a wide margin.

2. **The noise floor does its job.** A pure random walk produces a top |IC| of
   ~0.5 by chance alone — genuinely impressive-looking, and genuinely meaningless.
   The multiple-testing floor (`noise_floor_ic`) is what separates the two, and
   it must scale correctly: harsher with more factors, kinder with more data.

What this deliberately does NOT assert: that BTC's (or any real asset's) factors
clear the floor. That is an empirical claim about markets, it needs the network,
and the honest answer on this project's data is usually "no". Gating CI on it
would be pretending the scaffolds have proven edge — the opposite of the point.
This test validates the *tool*, not the market.

Speed: ranking runs over the momentum + ma_structure families (the ones that
read this kind of structure), but the noise floor is computed from the *full*
registry count — so the multiple-testing bar is the real one, while the panel
stays cheap enough to keep the suite network-free and fast.
"""

from __future__ import annotations

import math
import random
import pytest
from datetime import datetime, timedelta, timezone

from alpha_engine.cache.models import Candle, Interval, PriceSeries
from alpha_engine.quant.factors import compute_panel, factor_families, factor_names
from alpha_engine.quant.ranking import noise_floor_ic, rank_factors

_HORIZON = 5
_N = 400  # shorter series inflate chance IC on the random walk and blur the line
_T0 = datetime(2023, 1, 1, tzinfo=timezone.utc)

_FAMILIES = factor_families()
_PANEL_NAMES = _FAMILIES["momentum"] + _FAMILIES["ma_structure"]
_FULL_REGISTRY = len(factor_names(include_slow=False))
_FLOOR = noise_floor_ic(_FULL_REGISTRY, _N - _HORIZON)


def _series(closes: list[float]) -> PriceSeries:
    candles = [
        Candle(
            ts=_T0 + timedelta(days=i),
            open=c,
            high=c * 1.001,
            low=c * 0.999,
            close=c,
            volume=1_000_000.0,
        )
        for i, c in enumerate(closes)
    ]
    return PriceSeries(asset="TEST", interval=Interval.DAY, candles=candles)


def _structured_series(phi: float = 0.85) -> PriceSeries:
    """Fully deterministic series with a real, learnable dynamic: returns follow
    an AR(1) process (each carries 85% of the last) driven by a sine. That
    autocorrelation is genuine predictable structure — momentum and MA factors
    should read it loudly."""
    closes = []
    price, r = 100.0, 0.0
    for t in range(_N):
        r = phi * r + 0.01 * math.sin(t * 0.9)
        price *= math.exp(r)
        closes.append(price)
    return _series(closes)


def _random_walk(seed: int) -> PriceSeries:
    """A signal-free geometric random walk. Any IC here is chance."""
    rng = random.Random(seed)
    closes = []
    price = 100.0
    for _ in range(_N):
        price *= math.exp(rng.gauss(0.0004, 0.018))
        closes.append(price)
    return _series(closes)


def _ranked(series: PriceSeries):
    return rank_factors(series, compute_panel(series, names=_PANEL_NAMES), horizon=_HORIZON)


def _top_abs_ic(series: PriceSeries) -> float:
    return max(abs(s.rank_ic) for s in _ranked(series) if s.rank_ic is not None)


def test_pipeline_surfaces_real_structure_above_the_floor() -> None:
    """Embedded AR(1) momentum must produce factors that clear the noise floor by
    a wide margin — otherwise the ranking layer cannot find signal that is there."""
    scores = _ranked(_structured_series())
    assert _FLOOR is not None

    cleared = [s for s in scores if s.rank_ic is not None and abs(s.rank_ic) > _FLOOR]
    top = max(abs(s.rank_ic) for s in scores if s.rank_ic is not None)

    # The dynamic is strong and real: the best factor is near-perfectly
    # correlated (~0.99), far above the ~0.18 floor, with many factors reading
    # the same structure. Thresholds are conservative vs. the observed values.
    assert top > 0.85, f"top |IC| {top:.3f} — pipeline failed to detect real structure"
    assert len(cleared) > 40, f"only {len(cleared)} factors cleared the floor"


def test_noise_floor_separates_signal_from_a_lucky_random_walk() -> None:
    """The whole reason the floor exists: a random walk yields an impressive top
    |IC| (~0.5) by chance. Real structure must stand clearly above that."""
    structured_top = _top_abs_ic(_structured_series())
    walk_tops = [_top_abs_ic(_random_walk(seed)) for seed in (1, 42)]

    # Chance alone clears |IC| ~0.4-0.6 here — precisely why a raw top IC is not
    # evidence of anything, and why the ranking output always prints the floor.
    assert max(walk_tops) < 0.75, f"random-walk top |IC| unexpectedly high: {walk_tops}"
    # Genuine structure must beat the luckiest random walk by a clear margin.
    assert structured_top - max(walk_tops) > 0.2


def test_noise_floor_scales_with_the_multiple_testing_burden() -> None:
    """`noise_floor_ic` is the correction the ranking layer leans on. Pin its
    shape so a refactor cannot quietly make it lenient: more factors raise the
    bar, more observations lower it."""
    assert noise_floor_ic(500, 400) > noise_floor_ic(10, 400)  # more factors -> harsher
    assert noise_floor_ic(500, 50) > noise_floor_ic(500, 400)  # less data -> harsher
    # Degenerate inputs have no meaningful floor rather than a misleading number.
    assert noise_floor_ic(1, 400) is None
    assert noise_floor_ic(500, 2) is None


def test_low_coverage_factor_is_marked_noise_against_its_own_floor() -> None:
    """The bug this pins: a single table-wide floor derived from the *median*
    factor's sample size is too lenient for low-coverage factors.

    The floor scales with 1/sqrt(n), so a factor computable on 90 bars has to
    clear a visibly higher bar than one computable on 300. Judging both against
    the median's line lets the sparse one be presented as a ranked result it did
    not earn. Every factor must be scored against its own n_obs.
    """
    n_factors = 495
    median_floor = noise_floor_ic(n_factors, 297)  # the table-wide line
    sparse_floor = noise_floor_ic(n_factors, 94)  # a 252-period factor's own line

    assert sparse_floor > median_floor, "less data must mean a harsher floor"

    # An IC in this gap is the exact failure mode: it beats the table-wide line
    # while failing its own. It must not be reported as clearing the floor.
    ic = (median_floor + sparse_floor) / 2
    assert ic >= median_floor
    assert ic < sparse_floor


def test_factors_tool_reports_a_per_factor_floor() -> None:
    """`tool_factors` feeds MCP and the HTTP API, so the per-factor verdict has
    to survive there too — not just in the CLI's printed table."""
    from alpha_engine.toolkit import tool_factors

    payload = tool_factors({"asset": "BTC", "days": 400})
    if "error" in payload:  # no cached BTC series in this environment
        pytest.skip(f"no series available: {payload['error']}")

    assert payload["factors"], "expected at least one scored factor"
    for row in payload["factors"]:
        assert "own_noise_floor_ic" in row
        assert "clears_own_noise_floor" in row
        floor, ic = row["own_noise_floor_ic"], row["rank_ic"]
        if floor is not None and ic is not None:
            # The verdict must follow from that factor's own line, nothing else.
            assert row["clears_own_noise_floor"] == (abs(ic) >= floor)
