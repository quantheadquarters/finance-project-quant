# Architecture Deep Dive

This document explains how a symbol becomes a scored, recorded signal — and
why the pipeline is shaped the way it is. Read `context.md` first for the
non-negotiable rules; this is the mechanical tour.

## The data flow

```text
            ┌──────────────────────────────────────────────────────────────┐
            │                        alpha-engine scan BTC                  │
            └──────────────────────────────────────────────────────────────┘
                 │
                 ▼
  ┌───────────┐     ┌───────────┐     ┌─────────────┐     ┌────────────┐
  │ ingestion │ --> │   cache   │ --> │  analyzers  │ --> │  synthesis │
  │ (network) │     │ (local)   │     │ (pure fns)  │     │ (weighted  │
  └───────────┘     └───────────┘     └─────────────┘     │   vote)    │
   CoinGecko          data/cache/       trend  rsi         └─────┬──────┘
   Binance            price/…           macd   bollinger         │
   Yahoo              macro/…           mtf    s/r               ▼
   FRED               chain/…           vwap   volume      ┌────────────┐
   OANDA                                volatility regime  │ narrative  │
   Breeze/AngelOne/                                        │ (thesis    │
   Dhan (F&O chains)                                       │  prose)    │
                                                           └─────┬──────┘
                                                                 │
                                                                 ▼
                                       ┌──────────────────────────────────┐
                                       │            validation            │
                                       │ recorder (append-only JSONL) +   │
                                       │ outcomes + no-lookahead backtest │
                                       └──────────────────────────────────┘
```

Each stage may only look left. Analyzers read the cache, never the network;
synthesis reads sources, never raw data; the narrator reads a finished
signal and may only write its `thesis` string.

## The decision path (where every number comes from)

1. **Ingestion** normalizes a source's quirks into the cache models
   (`Candle`, `PriceSeries`, `OptionsChain`, `MacroObservation`). Fallback
   chains live here too: crypto tries CoinGecko Pro (if keyed), then keyless
   CoinGecko, then Binance.
2. **Analyzers** are pure functions producing `SignalSource` votes:
   direction + weight (0..1) + audit detail. Insufficient data degrades to
   weight 0, never an exception.
3. **Synthesis** (`synthesize.py`) computes the net direction by weighted
   vote, then calibrates confidence from three deterministic components:
   agreement quality between sources, per-analyzer historical reliability
   factors, and net-vote strength. One special case: the volatility-regime
   analyzer never votes a direction, but an *extreme* regime multiplies every
   other source's weight by 0.6 before the vote (a wild tape makes all
   directional reads less trustworthy).
4. **Invalidation** — "the price at which this view is wrong" — comes from
   recent swing structure (`trend_invalidation`), keyed to the *synthesized*
   direction. It is the schema's most important honesty mechanism.
5. **Narrative** renders the thesis. By default a deterministic template;
   with `--llm` and a key, a language model may *rephrase* it. The LLM never
   sees a number it could change — the schema fields are already frozen.

## Why an LLM can't corrupt a number

The `Signal` is fully assembled — direction, confidence, invalidation,
sources — before the narrator runs. The narrator returns a new `Signal` via
`model_copy(update={"thesis": ...})`; every other field is carried over
unchanged. A test pins this. If a change ever makes a number depend on the
narrator, that change is wrong by definition (context.md §2).

## How confidence calibration works

Confidence is not "how sure the analyzer feels" — it is a calibrated estimate
of hit probability, built from:

- **Agreement quality**: sources pointing the same way with similar weights
  raise it; a lone strong vote against a quiet field raises it less; open
  conflict drags it down.
- **Reliability factors**: each analyzer name carries a multiplier derived
  from backtested accuracy (`synthesize.py`). A read from a historically
  mediocre analyzer counts for less.
- **Whether it's working**: `record-stats` and `backtest` bucket outcomes by
  stated confidence. If the 0.6–0.8 bucket hits 45% of the time, the
  calibration curve shows it — miscalibration is *measured*, not hidden.

## The no-lookahead guarantee

All backtest signals are generated through one choke point:
`signal_at(series, t)` truncates candles to `[0..t]` **before** any analysis.
It also filters macro observations and news to what existed by bar `t`, and
admits a fundamental filing only after its publication timestamp
(`available_at`). The SEC source conservatively uses the next UTC day when it
provides only a filing date. Legacy fundamental records without that timestamp abstain in
a replay: guessing that a quarter-end value was public on quarter end would leak
future earnings. Tests pin every input by asserting the signal at bar `t` is
unchanged when future data is added. If you add an input to `signal_at`, truncate
it the same way and add the same pin.

## Portfolio layer

Cross-asset analytics sit above per-asset signals but below the dashboard:
`correlation.py` measures how the assets' daily returns co-move, and
`portfolio_signal.py` folds the latest signal per asset into one view — net
bias, conviction shares, diversification score, and concentration flags. All
deterministic; the dashboard just renders the result.

## Storage

- `data/cache/` — regenerable market data (gitignored). TTLs mark staleness;
  stale data is *served with a warning*, never silently trusted.
- `data/signals/` — the append-only signal log (JSONL, one file per day).
  Records are never mutated; outcome scoring happens by reading prices later.
  This log is the project's evidence base — treat it as write-once.
