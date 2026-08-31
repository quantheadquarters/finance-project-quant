# Bugs

Defects found, with their evidence. Each entry names the symptom, the
evidence, and the proposed fix, so it can be picked up cold.

---

## 1. Factor noise floor used the median factor's sample size, not each factor's own — FIXED 2026-08-31

**Where:** `src/alpha_engine/cli/main.py:1379-1381` (and the same three lines in
`src/alpha_engine/toolkit.py:183`).

**Symptom:** `factors` reports one noise floor for the whole table, computed from
the *median* factor's observation count. But coverage varies enormously across
the 495-factor registry — the 252-period factors only have valid values on ~14%
of bars, while short-window factors have ~84%. A single floor is therefore too
lenient for exactly the long-window factors that dominate the top of the ranking.

**Evidence** (`factors BTC`, 366 bars, 495 factors, median_obs=297 -> floor 0.2044):

| factor | IC | coverage | own n | own floor | verdict |
|---|---:|---:|---:|---:|---|
| `vol_percentile_252` | 0.3215 | 25.7% | 94 | 0.3633 | passes reported floor, **fails its own** |
| `ulcer_index_252` | 0.3198 | 31.4% | 114 | 0.3299 | passes reported floor, **fails its own** |

Both are printed in the ranked table above a floor they do not actually clear.
The top ~15 rows do clear their own floor, so the headline verdict line happens
to be correct on this run — but it is not guaranteed to be, since it compares
the best factor's IC against the *median* factor's floor.

**Why it matters:** the noise floor is the one thing standing between this
ranking and data-mined nonsense (see `noise_floor_ic`'s docstring). A floor
that is too lenient for the highest-ranked factors defeats its own purpose.

**Fix:** compute the floor per factor from that factor's own `n_obs`
(`FactorScore.n_obs` already exists), flag each row that fails its own line, and
compare the headline verdict against the top factor's own floor rather than the
median. Keep the aggregate line as context.

**Not a fix:** raising the global floor. Coverage differences are real and
per-factor is the correct granularity.

**Fixed 2026-08-31.** Every factor is now scored against `noise_floor_ic(k, its
own n_obs)`. The CLI table gained a `vs_floor` column, the JSON and the
`factors` tool gained `own_noise_floor_ic` / `clears_own_noise_floor`, and the
headline verdict compares the top factor against its own line. On BTC this
reclassified 385 of 495 factors as noise — see FINDINGS.md, 2026-08-31.
Pinned by `tests/test_factor_validation.py`.

---

## 2. `record-stats` hid how much of the track record it could not score — FIXED 2026-08-31

**Where:** `src/alpha_engine/cli/main.py:795-800`.

**Symptom:** scoring skips any record whose asset has no cached price series.
The count of skipped records is printed to **stderr only** — it never enters the
JSON payload. A consumer that reads the JSON (a dashboard, the MCP tool, any
script redirecting stderr) sees a `hit_rate` with no indication of how much of
the sample produced it.

**Evidence, 2026-08-31:** the signal log held 339 records across 7 assets, but
the local price cache held only BTC, AAPL and GOLD.

| | scored | resolved | hit_rate | avg realized return |
|---|---:|---:|---:|---:|
| Before backfill | 99 of 339 | 76 | **0.1184** | −1.92% |
| After backfill | 339 of 339 | 259 | **0.3050** | +1.32% |

The reported hit rate was wrong by a factor of ~2.6, and the sign of the average
return flipped. The exclusion is not random — it silently selects whichever
assets happen to be missing from a *local, regenerable* cache, so the number
changes depending on which machine runs it.

**Why it matters:** this is the metric that gates the ML phase and the one a
reader would quote as the live track record. It is exactly the silent-decay
failure the health layer exists to prevent, in the one place that has no
equivalent guard.

**Fix:** put `records_total`, `records_scored` and `records_skipped` (with the
skipped asset names) in the payload itself, and refuse to report a `hit_rate` at
all — or return it as `null` with a reason — when the scored fraction falls
below a threshold. A partial number presented as a whole one is worse than no
number.

**Workaround until then:** backfill every logged asset before trusting the
output, and read the stderr line.

```bash
python -c "from alpha_engine.ingestion import yahoo, coingecko
from alpha_engine.cache.interface import Cache
c = Cache()
for a in ['AAPL','MSFT','GOOGL','NVDA']: yahoo.fetch_daily(a, days=400, cache=c)
for a in ['BTC','ETH','SOL']: coingecko.fetch_daily(a, days=365, cache=c)"
```

Note the asymmetric windows: CoinGecko's free tier returns HTTP 401 above 365
days, while Yahoo accepts longer.

**Fixed 2026-08-31.** `annotate_coverage` in `validation/outcomes.py` now adds
`records_total`, `records_scored`, `records_skipped`, `scored_fraction` and
`skipped_assets` to the payload, and sets `hit_rate` to `null` with a
`hit_rate_suppressed` reason when under 95% of records could be scored. Applied
at both call sites — `cli/main.py` and `toolkit.py`, the latter being the MCP
surface an AI would quote. Pinned by `tests/test_factor_validation.py`.
