# Measured findings

What the engine has actually been shown to do, as opposed to what it was built
to do. Every number here came from running the code; the commands are included
so anyone can reproduce or refute them.

Update this file whenever a measurement changes. It is the answer to "does this
work?", and it is meant to be uncomfortable when the answer is no.

---

## 2026-07-27 — The engine has no measurable directional edge

**Verdict: +0.0% edge over 6,788 signals, 7 assets, 2.7–5 years of history.**

### What was measured

For every bar, the full live pipeline was replayed through `signal_at()` — the
same no-lookahead choke point `scan` uses — and each directional call was scored
against what price actually did over the next 10 bars (the swing horizon).

```bash
# Backfill deep history first: the cache holds ~90 bars by default, which
# leaves ~10 scorable signals after the 80-bar warmup.
python -c "from alpha_engine.ingestion import yahoo, binance
from alpha_engine.cache.interface import Cache
for a in ['AAPL','MSFT','GOOGL','NVDA']: yahoo.fetch_daily(a, days=1825, cache=Cache())
for a in ['BTC','ETH','SOL']: binance.fetch_daily(a, days=1825, cache=Cache())"

alpha-engine backtest BTC --days 1000 --per-analyzer --step 1 --no-refresh
```

### The result

Edge is measured against a **direction-matched base rate**: how often the asset
moved that way anyway. A bullish call on an asset that rises 56% of the time has
to beat 56%, not 50%.

| Asset | Signals | Bullish correct | Bearish correct | Base rate | **Edge** |
|---|---:|---:|---:|---:|---:|
| BTC | 830 | 51.3% | 45.2% | 53.3% | **−1.7%** |
| ETH | 854 | 46.8% | 51.9% | 47.0% | **−0.7%** |
| SOL | 829 | 49.9% | 49.5% | 50.9% | **−0.3%** |
| AAPL | 1080 | 58.2% | 45.8% | 56.1% | **+2.0%** |
| MSFT | 1051 | 52.6% | 46.4% | 52.5% | **−0.5%** |
| GOOGL | 1067 | 57.3% | 42.5% | 56.6% | **+0.1%** |
| NVDA | 1077 | 58.4% | 42.7% | 57.1% | **+0.7%** |
| **All** | **6,788** | | | | **+0.0%** |

The spread from −1.7% to +2.0% is noise around zero. This is exactly what
`AGENTS.md` has always claimed ("analyzers are honest scaffolds, ~coin-flip");
it is now measured at scale rather than asserted.

### Two things that nearly got reported as edge

Recording these because both are easy to fall for, and one of them was caught
only on a second look.

**1. Conditioning on survival.** Scoring only the signals that were *not*
stopped out gives a +10% edge across all four assets tested. It is an artifact:
a bullish call that drops gets stopped out and removed from the sample, so
"survivors finished up more often" is close to tautological. Never score a
subset selected by the outcome.

**2. Comparing against 50%.** Several analyzers look near-50% and therefore
harmless. But AAPL rose in 56.1% of 10-bar windows over this period, so 50% is
*worse than doing nothing*. The base rate is the bar, not a coin.

### CORRECTION (same day): the invalidation level is NOT destroying value

An earlier revision of this file claimed the stop was "converting a coin flip
into a loss". That was wrong, and the error is worth keeping on the record
because it is the third false positive this measurement has produced.

The claim came from comparing a **hit rate** against a base rate. `hit` counts
any touched stop as a total miss regardless of magnitude, so the metric punishes
stops by construction — it cannot tell a 1% scratch from a 20% rout.

Measured properly, in return terms, on the same 5,959 signals:

| Asset | No stop | With stop | Stop cost |
|---|---:|---:|---:|
| BTC | +0.290% | +0.216% | −0.073% |
| ETH | +0.989% | +0.685% | −0.304% |
| AAPL | −0.059% | +0.063% | **+0.122%** |
| MSFT | −0.280% | −0.347% | −0.067% |
| NVDA | +0.599% | +0.618% | **+0.019%** |
| GOOGL | −0.084% | −0.036% | **+0.048%** |
| **All** | **+0.215%** | **+0.184%** | **−0.031%** |

It helps on three assets and hurts on three. −0.031% overall is noise. The stop
is roughly free, and it buys a bounded worst case — which is worth having.

**The lesson, again: pick the metric that answers the question you asked.** Hit
rate answers "was the call right"; it does not answer "did the rule make money",
and using it for the second question invents a defect that is not there.

The median stop also sits *wider* than the median 10-bar drawdown (1.6–1.9 ATR),
so "too tight" was wrong on the mechanics as well as the outcome.

### Per-analyzer, without the stop (AAPL, base rate 56.1%)

| Analyzer | Signals | Correct | vs base |
|---|---:|---:|---:|
| multi_timeframe | 1103 | 52.6% | −3.6% |
| vwap | 1163 | 52.3% | −3.9% |
| volume | 1163 | 51.9% | −4.2% |
| macd | 1163 | 51.2% | −4.9% |
| support_resistance | 953 | 49.9% | −6.2% |
| bollinger | 795 | 45.9% | −10.2% |
| rsi | 146 | 45.2% | −10.9% |

Read with care: this column compares against a *raw* base rate, which penalises
bearish calls on an asset that spent the window rising. The direction-matched
number in the table above (+0.0%) is the fair one. What survives either reading
is the ordering — `rsi` and `bollinger` are the weakest inputs by a clear
margin, and both are worth removing or re-thinking before anything else is added.

### The analyzers systematically contradict each other — and fixing that changes nothing

Two of the eight analyzers are **mean-reverting** (`rsi`: oversold → bullish;
`bollinger`: below the lower band → bullish). The other six are
**trend-following**. Measured agreement with the trend anchor on AAPL:

| Analyzer | Shared signals | Agrees with trend |
|---|---:|---:|
| multi_timeframe | 370 | 83.5% |
| vwap | 388 | 74.7% |
| volume | 388 | 70.4% |
| macd | 388 | 61.6% |
| support_resistance | 318 | 45.9% |
| **bollinger** | 264 | **18.2%** |
| **rsi** | 49 | **2.0%** |

`rsi` opposes the trend anchor 98% of the time. That is not a bug — it is what a
contrarian indicator does — but it means the engine averages two opposite
philosophies into one weighted vote, where they cancel. It looked like the
explanation for the mediocre blend.

**It is not.** Rebuilding the blend without `rsi` and `bollinger`, scored on the
same bars:

| Asset | Base rate | All 8 | Without mean-reversion | Change |
|---|---:|---:|---:|---:|
| BTC | 53.3% | 47.5% | 48.2% | +0.8% |
| AAPL | 56.1% | 52.2% | 52.5% | +0.2% |
| NVDA | 57.1% | 52.6% | 52.7% | +0.1% |
| MSFT | 52.5% | 49.1% | 48.6% | −0.4% |
| **All (n=2,063)** | | **50.5%** | **50.7%** | **+0.1%** |

+0.1% is noise, and both versions remain below every base rate.

**This is the deepest result in the file.** The problem is not the blend, not the
stop, and not which analyzers are included. Averaging noise with anti-correlated
noise gives noise; removing the anti-correlated noise also gives noise. There is
nothing to blend.

It also retires two recommendations this document previously made — "fix the
invalidation level" and "drop rsi and bollinger". Both were plausible, both were
measured, and both were wrong. Recomposing the existing inputs is a dead end;
the only moves left are to find an input that actually predicts, or to accept
the engine as a research instrument and stop trying to make it profitable.

---

## Every claim this measurement got wrong before getting it right

Kept deliberately. Four plausible findings died under a second look, and the
pattern in all four is the same: **the metric answered a different question than
the one being asked.**

| Claim | Why it was wrong |
|---|---|
| "+10% edge among surviving signals" | Survivorship. Losers get stopped out and leave the sample. |
| "Analyzers are near 50%, so harmless" | AAPL rose 56.1% of the time. 50% is worse than nothing. |
| "The invalidation level is destroying value" | Measured by hit rate, which counts any stop-out as a total miss. In return terms it costs −0.031% — noise. |
| "Drop rsi and bollinger to improve the blend" | Measured: +0.1%. Nothing to improve. |

If a fifth reading looks like edge, assume it is one of these until shown
otherwise.

---

## 2026-08-31 — 385 of 495 factors were being ranked above a floor they had not cleared

**Verdict: the factor ranking's multiple-testing guard was applied at the wrong
granularity. Only 110 of 495 factors clear their own noise floor on BTC.**

### What was wrong

`noise_floor_ic` answers: what |IC| would the *best* of k purely random factors
reach on n observations? It is the one thing separating this ranking from
data-mined nonsense, and it scales with `1/sqrt(n)` — less data means a higher
bar.

The CLI and the `factors` tool both computed **one** floor for the whole table,
from the **median** factor's observation count. But coverage across the registry
is wildly uneven: a 252-period factor is computable on ~14% of a 366-bar series,
a 10-period one on ~97%. A single median-derived line is therefore too lenient
for exactly the long-window factors that crowd the top of the ranking.

### The result

On `factors BTC` (366 bars, 495 factors), the table-wide floor was **0.2044**
from a median sample of 297. Judged against their own samples:

| factor | \|IC\| | coverage | own n | own floor | old verdict | correct verdict |
|---|---:|---:|---:|---:|---|---|
| `slope_sma_252` | 0.6856 | 14.2% | 42 | 0.5436 | clears (vs 0.2044) | clears, but barely |
| `vol_percentile_252` | 0.3215 | 25.7% | 94 | 0.3633 | clears | **noise** |
| `ulcer_index_252` | 0.3198 | 31.4% | 114 | 0.3299 | clears | **noise** |

Across the full table: **385 of 495 factors are indistinguishable from noise**
at their own sample size; 110 clear their line. The headline verdict was also
comparing the top factor's IC against the *median* factor's floor — on this run
the top factor's real bar is 0.5436 on 42 observations, not 0.2044 on 297. It
still clears, but with far less room than the output implied.

```bash
alpha-engine factors BTC --top 0     # vs_floor column, per-factor verdict
alpha-engine factors BTC --json      # own_noise_floor_ic, clears_own_noise_floor
```

### Why it is recorded here

No number in this file changed — the +0.0% edge measurement never went through
the factor ranking. What changed is how much of the factor registry can be
described as promising: far less than the previous output suggested. That is a
result about the instrument rather than the market, which is exactly the kind of
thing that otherwise goes unrecorded.

Fixed in `cli/main.py` and `toolkit.py`; pinned by
`tests/test_factor_validation.py::test_low_coverage_factor_is_marked_noise_against_its_own_floor`.

---

## 2026-08-31 — The factor registry was swept out-of-sample. It found nothing.

**Verdict: across 7 assets, factors replicated out-of-sample 3.7 percentage
points WORSE than shuffled noise. No edge.**

FUTURE_WORK.md named this as the one remaining path to an actual edge: the
504-factor registry "has never been run systematically". It has now.

### Method

`scripts/factor_sweep.py`. Per asset: split history 60/40, rank all 495 factors
on train, keep only those clearing their own per-factor noise floor, re-measure
the survivors on test, and require the same IC sign. Then require replication
across assets. The panel is computed once on full history and sliced — every
factor is lookahead-pinned, so a factor's value at bar i uses only bars <= i,
which preserves the warmup a 252-period factor needs.

```bash
python scripts/factor_sweep.py --json sweep.json
```

### The result that was nearly reported as edge

The first run reported **70.3% out-of-sample sign agreement, z = +9.52** against
an assumed null of 50%, plus 41 factors "confirmed" on 2+ assets. It looked
overwhelming.

It was wrong, in two compounding ways:

**1. The assumed null was nowhere near 50%.** Shuffling the test window's closes
destroys any factor-to-future relation while leaving the factors untouched. Run
through that, the same procedure still returns ~74% sign agreement. Slow,
autocorrelated factors scored against overlapping forward returns keep their IC
sign across a split for reasons that have nothing to do with prediction.

**2. The 41 "confirmed" factors were one idea, not 41.** `dist_sma_120`,
`dist_ema_120`, `dist_vwap_120`, `slope_sma_90`, `slope_ema_90` and `mom_90` are
all "where is price relative to its long average". The z-score treated 548
correlated measurements as independent trials.

### The corrected numbers

| Asset | sign agreement | shuffled null | delta |
|---|---:|---:|---:|
| BTC | 72.8% | 84.8% | **−12.0%** |
| ETH | 77.5% | 82.0% | **−4.4%** |
| SOL | 83.3% | 81.9% | +1.5% |
| AAPL | 38.1% | 42.3% | **−4.2%** |
| MSFT | 60.9% | 67.4% | **−6.5%** |
| GOOGL | 30.5% | 36.3% | **−5.8%** |
| NVDA | 96.9% | 91.4% | +5.5% |
| **Pooled** | **65.7%** | **69.4%** | **−3.7%** |

Factors "confirmed": **170 real vs 179 expected from shuffled noise.** Five of
seven assets are negative. The registry does not beat its own null.

### Why this is the third entry in this file rather than the first

This is the same failure mode as the two in the 2026-07-27 entry — a
plausible-looking number that survives until it is tested against the right
null. The lesson is now structural rather than remembered: the permutation
control runs on **every** invocation of the sweep and prints beside the real
number, so the two cannot be reported separately or drift apart. `--permutations 0`
disables it and the output says in plain text that the result is uninterpretable.

Pinned by `tests/test_factor_validation.py::test_sweep_shuffle_control_preserves_train_and_permutes_test`,
because a shuffle that silently became a no-op would compare the real run
against itself, report a delta of zero forever, and look exactly like a working
control.

**This does not close the search.** It rules out this registry, at this horizon,
on this much history, judged this way. It does not rule out other inputs — but
it does mean the +0.0% headline stands, and now stands against a systematic
attempt to overturn it rather than an absence of one.

---

## What this does and does not mean

**It does not mean the project failed.** The engine was built to answer this
question honestly, and it did. Most systems like this never find out, because
they are never measured against a base rate on enough samples to tell.

**It does mean nothing here should be traded**, and that the next work is
subtractive rather than additive: fix the invalidation level, drop or repair the
two worst analyzers, and re-measure. Adding a ninth analyzer to eight that carry
no signal produces nine that carry no signal.

**The measurement is now cheap to repeat.** That is the real asset. Any change
to an analyzer can be scored against 6,788 samples in minutes, so the next
version of this file can say something different — and be believed.
