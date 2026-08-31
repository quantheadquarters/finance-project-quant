#!/usr/bin/env python3
"""Systematic out-of-sample sweep of the factor registry.

FUTURE_WORK.md names this as the one remaining path to an actual edge: the
504-factor registry "has never been run systematically". This is that run.

The method, and why each part is there:

1. **Split each asset's history into train and test.** Ranking and confirming on
   the same bars is what produces |IC| ~ 0.9 headlines that mean nothing.

2. **Rank on train, keep only factors clearing their own noise floor.** The floor
   is `sqrt(2 ln k)/sqrt(n)` — what the BEST of k random factors reaches by luck
   on n observations. It is computed per factor, from that factor's own sample,
   because coverage across the registry ranges from ~14% to ~97% and the floor
   scales with 1/sqrt(n).

3. **Re-measure the survivors on test, and require the SAME SIGN.** A factor that
   predicted up in-sample and down out-of-sample has not replicated; it has
   changed its mind.

4. **Require replication across assets.** One asset clearing is unremarkable —
   with 7 assets and a coin-flip process, something usually clears somewhere.

5. **Compare every number against a PERMUTATION NULL, not against 50%.** This is
   the part that makes the rest trustworthy, and it was added after the first
   run of this script reported 70.3% sign agreement at "z=+9.52" — a result that
   evaporated the moment it was tested properly.

   Shuffling the test-window closes destroys any real factor->future relation
   while leaving the factors themselves untouched. Run through that, the same
   procedure still returns ~85% sign agreement. The null for this design is
   nowhere near 50%: slow, autocorrelated factors scored against overlapping
   forward returns keep their IC sign across a split for reasons that have
   nothing to do with prediction.

   So the honest statistic is **real minus shuffled**, and on the first proper
   run that difference was negative — the real data replicated *less* than
   noise did. Any future claim of edge from this script has to clear its own
   shuffled control, printed alongside it every run.

The panel is computed once on full history and then sliced. Every factor in the
registry is lookahead-pinned (tests/test_factors.py::test_no_lookahead), so a
factor's value at bar i uses only bars <= i. Slicing therefore preserves the
warmup a 252-period factor needs, which recomputing on a short test window would
destroy. Forward returns are computed inside each slice, so the last `horizon`
bars of train never see test.

Usage:
    python scripts/factor_sweep.py                     # all cached assets
    python scripts/factor_sweep.py --assets BTC ETH    # a subset
    python scripts/factor_sweep.py --json out.json     # machine-readable
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections import defaultdict

from alpha_engine.cache.interface import Cache
from alpha_engine.cache.models import PriceSeries
from alpha_engine.quant.factors import FACTOR_REGISTRY, compute_panel, factor_names
from alpha_engine.quant.ranking import noise_floor_ic, rank_factors

DEFAULT_ASSETS = ["BTC", "ETH", "SOL", "AAPL", "MSFT", "GOOGL", "NVDA"]


def _slice(series: PriceSeries, lo: int, hi: int) -> PriceSeries:
    """A PriceSeries over candles[lo:hi], keeping identity fields intact."""
    return PriceSeries(
        asset=series.asset,
        interval=series.interval,
        candles=series.candles[lo:hi],
        fetched_at=series.fetched_at,
    )


def _shuffled(series: PriceSeries, split: int, seed: int) -> PriceSeries:
    """The same series with the TEST-window closes randomly reordered.

    This is the control. Reordering closes severs any factor->future-return
    relation while leaving each factor's own values and autocorrelation intact,
    so whatever survives this run is an artifact of the procedure rather than
    evidence about the market.
    """
    rnd = random.Random(seed)
    head, tail = series.candles[:split], series.candles[split:]
    closes = [c.close for c in tail]
    rnd.shuffle(closes)
    return PriceSeries(
        asset=series.asset,
        interval=series.interval,
        candles=head + [c.model_copy(update={"close": cl}) for c, cl in zip(tail, closes)],
        fetched_at=series.fetched_at,
    )


def sweep_asset(
    series: PriceSeries,
    names: list[str],
    horizon: int,
    train_frac: float,
    shuffle_seed: int | None = None,
) -> dict:
    """Rank on train, confirm on test, for one asset.

    With `shuffle_seed` set, the test window's closes are permuted first — the
    null run whose numbers the real run has to beat.
    """
    n = len(series.candles)
    split = int(n * train_frac)
    if split < 60 or n - split < 60:
        return {"asset": series.asset, "error": f"too little history ({n} bars)"}

    if shuffle_seed is not None:
        series = _shuffled(series, split, shuffle_seed)

    # One lookahead-clean panel, then sliced — see the module docstring.
    panel = compute_panel(series, names=names)
    train_panel = {k: v[:split] for k, v in panel.items()}
    test_panel = {k: v[split:] for k, v in panel.items()}

    train_scores = rank_factors(_slice(series, 0, split), train_panel, horizon=horizon)
    test_scores = rank_factors(_slice(series, split, n), test_panel, horizon=horizon)
    test_by_name = {s.name: s for s in test_scores}

    k_train = len(train_scores)
    survivors = []
    for s in train_scores:
        if s.rank_ic is None:
            continue
        floor = noise_floor_ic(k_train, s.n_obs)
        if floor is not None and abs(s.rank_ic) >= floor:
            survivors.append(s)

    # The test-side burden is over the survivors actually retested, not the whole
    # registry: those are the only hypotheses carried forward.
    k_test = max(len(survivors), 2)
    confirmed, sign_agreements, comparable = [], 0, 0
    for s in survivors:
        t = test_by_name.get(s.name)
        if t is None or t.rank_ic is None:
            continue
        comparable += 1
        same_sign = (s.rank_ic > 0) == (t.rank_ic > 0)
        sign_agreements += int(same_sign)
        floor = noise_floor_ic(k_test, t.n_obs)
        if same_sign and floor is not None and abs(t.rank_ic) >= floor:
            confirmed.append(
                {
                    "factor": s.name,
                    "family": getattr(FACTOR_REGISTRY.get(s.name), "family", None),
                    "train_ic": round(s.rank_ic, 4),
                    "test_ic": round(t.rank_ic, 4),
                    "test_n": t.n_obs,
                    "test_floor": round(floor, 4),
                }
            )

    return {
        "asset": series.asset,
        "bars": n,
        "train_bars": split,
        "test_bars": n - split,
        "factors_scored": k_train,
        "cleared_train_floor": len(survivors),
        "retested": comparable,
        "sign_agreement": round(sign_agreements / comparable, 4) if comparable else None,
        "confirmed": sorted(confirmed, key=lambda d: -abs(d["test_ic"])),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--assets", nargs="+", default=DEFAULT_ASSETS)
    ap.add_argument("--horizon", type=int, default=10, help="forward-return bars")
    ap.add_argument("--train-frac", type=float, default=0.6)
    ap.add_argument("--family", default=None, help="restrict to one factor family")
    ap.add_argument("--min-assets", type=int, default=2, help="replication threshold")
    ap.add_argument(
        "--permutations",
        type=int,
        default=3,
        help="shuffled control runs per asset (0 disables; result is then uninterpretable)",
    )
    ap.add_argument("--json", default=None, help="write full results to this path")
    args = ap.parse_args()

    names = factor_names(families=[args.family] if args.family else None)
    if not names:
        print(f"unknown factor family '{args.family}'", file=sys.stderr)
        return 2

    cache = Cache()
    results, missing = [], []
    for asset in args.assets:
        series, _stale = cache.get_price(asset, "1d")
        if series is None:
            missing.append(asset)
            continue
        print(f"[sweep] {asset}: {len(series.candles)} bars...", file=sys.stderr)
        real = sweep_asset(series, names, args.horizon, args.train_frac)
        # The control runs alongside every real run, so the two can never drift
        # apart or be reported separately by accident.
        if "error" not in real and args.permutations:
            nulls = []
            for seed in range(args.permutations):
                print(f"[sweep]   null {seed + 1}/{args.permutations}...", file=sys.stderr)
                nulls.append(
                    sweep_asset(series, names, args.horizon, args.train_frac, shuffle_seed=seed)
                )
            agree = [x["sign_agreement"] for x in nulls if x.get("sign_agreement") is not None]
            real["null_sign_agreement"] = round(sum(agree) / len(agree), 4) if agree else None
            real["null_confirmed"] = round(sum(len(x["confirmed"]) for x in nulls) / len(nulls), 1)
        results.append(real)

    if missing:
        print(
            f"[sweep] no cached prices for: {', '.join(missing)} "
            "(these assets are absent from the sweep, not evidence of anything)",
            file=sys.stderr,
        )

    ok = [r for r in results if "error" not in r]
    if not ok:
        print("[sweep] no asset had enough history", file=sys.stderr)
        return 1

    # Cross-asset replication: the filter that separates a real factor from the
    # one asset where luck happened to land.
    per_factor: dict[str, list[dict]] = defaultdict(list)
    for r in ok:
        for c in r["confirmed"]:
            per_factor[c["factor"]].append({"asset": r["asset"], **c})

    replicated = {f: v for f, v in per_factor.items() if len(v) >= args.min_assets}

    agreements = [r["sign_agreement"] for r in ok if r["sign_agreement"] is not None]
    pooled_sign = sum(agreements) / len(agreements) if agreements else None

    print("\n" + "=" * 78)
    print(f"FACTOR SWEEP — {len(ok)} assets, horizon={args.horizon}, train={args.train_frac:.0%}")
    print("=" * 78)
    print(
        f"\n{'asset':8}{'bars':>7}{'train_ok':>10}{'confirmed':>11}"
        f"{'sign_agree':>12}{'null':>9}{'delta':>9}"
    )
    print("-" * 78)
    for r in ok:
        sa, nu = r["sign_agreement"], r.get("null_sign_agreement")
        sa_s = f"{sa:.1%}" if sa is not None else "n/a"
        nu_s = f"{nu:.1%}" if nu is not None else "n/a"
        d_s = f"{sa - nu:+.1%}" if (sa is not None and nu is not None) else "n/a"
        print(
            f"{r['asset']:8}{r['bars']:>7}{r['cleared_train_floor']:>10}"
            f"{len(r['confirmed']):>11}{sa_s:>12}{nu_s:>9}{d_s:>9}"
        )

    null_agrees = [r["null_sign_agreement"] for r in ok if r.get("null_sign_agreement") is not None]
    pooled_null = sum(null_agrees) / len(null_agrees) if null_agrees else None

    print(
        f"\nPooled out-of-sample sign agreement: "
        f"{f'{pooled_sign:.1%}' if pooled_sign is not None else 'n/a'}"
    )
    if pooled_null is None:
        print("  No permutation control was run (--permutations 0), so this number")
        print("  cannot be interpreted. It is NOT to be compared against 50%.")
    else:
        delta = pooled_sign - pooled_null
        print(f"Same statistic on SHUFFLED returns:   {pooled_null:.1%}")
        print(f"Difference (real - null):             {delta:+.1%}")
        print()
        print("  The shuffled run carries no information by construction, so its rate")
        print("  is what this procedure produces from nothing. Only the DIFFERENCE")
        print("  means anything. The null is nowhere near 50%: slow, autocorrelated")
        print("  factors scored on overlapping forward returns keep their IC sign")
        print("  across a split for reasons that have nothing to do with prediction.")
        if delta <= 0:
            print()
            print("  >> The real data replicated NO BETTER THAN NOISE.")
            print("     This sweep found no edge. That is the honest reading.")
        else:
            print()
            print("  >> Real exceeds the null. Necessary, not sufficient: confirm on")
            print("     history this sweep has never touched before believing it.")

    null_conf = [r["null_confirmed"] for r in ok if r.get("null_confirmed") is not None]
    if null_conf:
        real_conf = sum(len(r["confirmed"]) for r in ok)
        print(
            f"\nFactors 'confirmed': {real_conf} real vs "
            f"{sum(null_conf):.0f} expected from shuffled noise"
        )

    print(f"\nFactors confirmed on >= {args.min_assets} assets: {len(replicated)}")
    if replicated:
        print(f"\n{'factor':28}{'assets':>8}  {'test ICs'}")
        print("-" * 78)
        for f, hits in sorted(replicated.items(), key=lambda kv: -len(kv[1]))[:25]:
            ics = ", ".join(f"{h['asset']}:{h['test_ic']:+.3f}" for h in hits)
            print(f"{f:28}{len(hits):>8}  {ics}")
        print("\nThese replicated out-of-sample across assets. That is a reason to")
        print("look harder, not a reason to trade. Confirm on history this sweep")
        print("has never seen before believing any of it.")
    else:
        print("\nNothing replicated across the required number of assets.")
        print("That is the expected result if the registry carries no signal, and")
        print("it is a finding worth recording rather than a run that failed.")

    print("\nRESEARCH ONLY. Not financial advice.")

    if args.json:
        with open(args.json, "w") as fh:
            json.dump(
                {
                    "horizon": args.horizon,
                    "train_frac": args.train_frac,
                    "assets": results,
                    "missing_assets": missing,
                    "pooled_sign_agreement": pooled_sign,
                    "pooled_null_sign_agreement": pooled_null,
                    "permutations": args.permutations,
                    "replicated": replicated,
                },
                fh,
                indent=2,
            )
        print(f"\n[sweep] full results written to {args.json}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
