# CONTEXT.md

This file orients anyone (human or AI assistant like Claude Code) working on this
project. Read it fully before making changes. It explains what the project is, the
non-negotiable design rules, how the pieces fit, and how to work on it safely.

---

## Current research handoff — remind Shubh first (2026-09-23)

At the start of the next work session, remind Shubh of this plan before proposing
new alpha code:

1. The four-stock momentum candidate is implemented but **rejected**: price-only
   returned +200.89%, price plus SEC revenue confirmation returned +135.31%, and
   the same-period equal-weight buy-and-hold benchmark returned +553.77%.
2. The first priority is access to survivorship-clean, point-in-time US equity
   history containing delisted stocks, corporate actions, and historical universe
   membership. Prefer running the frozen rule inside QuantConnect Cloud; CRSP/WRDS
   or licensed Sharadar data are alternatives. No such account or dataset was
   available locally as of this handoff. QuantConnect's downloaded data may not be
   converted into this repo under its local-data license.
3. Once access exists, rerun the **unchanged** rule across a dynamic universe with
   delisting returns, next-open execution, costs, a broad-market benchmark, and a
   risk-matched benchmark. Do not tune it after seeing evaluation results.
4. Only after that baseline should the project test a small preregistered set:
   broader momentum, post-earnings drift, quality/profitability confirmation, and
   properly timestamped historical news sentiment.
5. Keep a sealed holdout and then a forward paper record. Never promote a candidate
   into the live engine unless its net excess return survives both gates.

The implementation, exact caveats, and promotion gates are in
`docs/ALPHA_RESEARCH_PLAN.md`; measured failures belong in `FINDINGS.md`. Update
or remove this handoff when the broad-universe rerun is complete.

---

## 1. What this project is

An open, deterministic **research engine** that turns market data into structured,
confidence-scored signals across multiple markets (crypto first, then US equities
and macro, then Indian equities and F&O). It is three things at once, by design:

1. A **personal research/trading engine** the author controls end to end.
2. A **portfolio piece** demonstrating clean multi-agent/data-pipeline architecture.
3. A **free, clonable community tool** anyone can run and extend.

It is **not** a product that sells signals, not a managed-money service, and not an
advisory. It produces directional *research views*, not buy/sell recommendations.

### What it explicitly is NOT, and why

- **Not investment advice.** This framing is legal and deliberate. In India,
  charging retail users for buy/sell recommendations triggers SEBI Research Analyst
  registration. The project stays free and non-advisory to stay clear of that.
  Every signal carries a research-only disclaimer. Do not remove it.
- **Not a profit claim.** The current analyzers are transparent scaffolds, not
  proven alpha. Whether any signal has edge is a question the validation layer
  answers with data, never something the marketing or docs assert.

---

## 2. The cardinal design rule (read this twice)

**Decision-bearing numbers are computed by deterministic, tested Python. A language
model may write ONLY the `thesis` prose string, and may never set or change a
number.**

Concretely:

- `direction`, `confidence`, `invalidation_level`, and every `SignalSource.weight`
  are produced by pure functions. Same input, same output, always.
- No analyzer makes a network call. Analyzers read from the `Cache`. Fetching lives
  in `ingestion/`.
- No randomness in `analyzers/` or `synthesis/`.
- The LLM lives only in `narrative/`, is optional, and is gated behind a
  user-supplied key. With no key, a deterministic template writes the thesis.

If a proposed change makes a number depend on an LLM or on randomness, it is wrong
and belongs in a different layer or not at all. This rule is what makes the system
backtestable and trustworthy. It is the heart of the project.

---

## 3. Architecture and data flow

```text
  ingest          cache             analyze            synthesize        narrate
 (sources)  ->  (local store)  ->  (deterministic) ->  (weighted vote) -> (prose)
                                                                              |
                                                                              v
                                                                          Signal
                                                                              |
                                                                              v
                                                                       validation
                                                                  (record + backtest)
```

Each stage is one directory under `src/alpha_engine/`:

| Layer        | Directory      | Responsibility                                              | May call network? | May use LLM? |
| ------------ | -------------- | ----------------------------------------------------------- | ----------------- | ------------ |
| Schema       | `schema/`      | The `Signal` contract. The spine everything depends on.     | no                | no           |
| Cache        | `cache/`       | Read interface + local store. Analyzers read from here.     | no                | no           |
| Ingestion    | `ingestion/`   | Source adapters that normalize external data into cache.    | YES               | no           |
| Analyzers    | `analyzers/`   | Deterministic, pure-function specialists.                   | no                | no           |
| Synthesis    | `synthesis/`   | Folds analyzer `SignalSource`s into one `Signal`.           | no                | no           |
| Narrative    | `narrative/`   | Writes the `thesis` string. Templated; optional LLM.        | only LLM call     | YES (only)   |
| Validation   | `validation/`  | Immutable signal recording + outcome scoring + backtests.   | no                | no           |
| CLI          | `cli/`         | `scan`, `backtest`, `record-stats` commands.                | via ingestion     | via narrative|

### The Signal schema is the contract

Everything compiles against `schema/signal.py`. Fields: `asset`, `market`,
`direction`, `confidence` (0-1), `timeframe`, `signal_sources` (list),
`invalidation_level`, `thesis`, `timestamp` (UTC), `schema_version`. Changing a
field changes every downstream layer, so bump `SCHEMA_VERSION` deliberately and
update consumers. The `invalidation_level` field ("the price at which this view is
wrong") is the most important honesty mechanism in the schema; keep it meaningful.

---

## 4. Repository layout

```text
src/alpha_engine/
  schema/signal.py          Signal, SignalSource, enums. Read first.
  cache/models.py           Normalized data shapes (Candle, PriceSeries, MacroObservation).
  cache/interface.py        Cache (public read API) + LocalStore + TTL/staleness.
  ingestion/coingecko.py    Keyless crypto source. The zero-setup default path.
  ingestion/yahoo.py        Keyless US equity daily candles (Yahoo chart endpoint).
  ingestion/fred.py         US macro series; free-key-gated, degrades gracefully.
  analyzers/crypto_trend.py First deterministic analyzer (dual-MA trend + momentum).
  analyzers/equity_trend.py Equity price structure; delegates to trend core for now.
  analyzers/macro_context.py Tightening/easing posture as a capped contextual tilt.
  quant/features.py         ~50 deterministic features (returns, trend, vol, volume, stats).
  quant/models.py           Kalman fair value, GARCH(1,1) vol, 2-state HMM regime — pure Python.
  quant/report.py           Scored quant report + indicators (ADX, Keltner, volume profile).
  synthesis/synthesize.py   Weighted-vote synthesis into a Signal.
  narrative/narrator.py     Templated thesis; optional-LLM hook.
  validation/recorder.py    Append-only JSONL signal log (data/signals/). Never mutates.
  validation/outcomes.py    Scores signals vs. later prices; calibration summary.
  validation/backtest.py    No-lookahead replay; `signal_at` is the truncation choke point.
  cli/main.py               `scan`, `report`, `backtest`, `record-stats` entry points.
tests/test_core.py          Determinism + schema validation tests.
tests/test_quant.py         Feature/model/report determinism and edge-case tests.
tests/test_validation.py    Recorder immutability, scoring rules, no-lookahead pin.
tests/test_markets.py       Yahoo/FRED parsing, equity + macro analyzers, blending.
pyproject.toml              Packaging, deps, pytest/ruff config.
README.md                   User-facing overview + capability matrix.
CONTRIBUTING.md             Contributor rules (mirrors the cardinal rule).
.env.example                Optional keys (all free tiers). Default path needs none.
```

---

## 5. Current status (Phases 1–6 complete)

**Working:** end-to-end pipeline across all configured markets. `scan BTC`
(CoinGecko), `scan AAPL` (Yahoo), and `scan RELIANCE.NS` (Yahoo) all fetch,
cache, analyze, synthesize, narrate, append to the immutable log, and print
JSON — no keys. With a free FRED key, equity scans additionally blend a
macro-context tilt (fed funds trend, CPI YoY, unemployment) through the
multi-source synthesis seam; without one they degrade gracefully to trend-only.
Indian F&O analytics (PCR, max-pain, OI shifts) run on any normalized chain
in the cache, with live broker adapters for Breeze, Angel One, and Dhan.
`backtest <ASSET>` replays cached history for any price-series market with no
lookahead and reports hit rate, average captured move, and a calibration curve.
`record-stats` scores the live signal log against outcomes. `scan-all` and
`batch` run multi-asset scans across all configured markets. The read-only
dashboard serves the latest recorded signals and outcome stats. `report <ASSET>`
prints a deterministic quant metrics report — regime (2-state HMM blended with
drift), trend/momentum/volume scores, GARCH(1,1) volatility forecast, Kalman
fair value, classic indicators, and ~50 features (`--json` for all of them).
325 unit tests pass.

Confidence calibration has been improved: the synthesis layer now factors in
source reliability and agreement quality, so high confidence requires both
strong agreement AND historically reliable source types. The source-count cap
ensures few sources produce honestly uncertain confidence levels.

The LLM narrator is optional and gated behind a user-supplied key. It rewrites
thesis prose but is re-validated to never change a number on the Signal.

Note one deviation from the original build plan: equity candles come from Yahoo's keyless chart
endpoint, not Finnhub — Finnhub's free tier no longer serves stock candles, and
keyless beats key-gated per the rules below.

**Known honest limitations (documented, not hidden):**

- The trend analyzer is a scaffold heuristic, not proven alpha. The first backtest
  confirms it: ~50% hit rate on 90 days of BTC. Confidence calibration has been
  improved to better reflect actual signal reliability, but the underlying
  analyzers still need edge.
- Free data sources (CoinGecko keyless) rate-limit with HTTP 429. The cache exists
  precisely to minimize hits; wait and retry on 429. Tests are network-free.

---

## 6. How to work on this project

### Environment (macOS, Homebrew Python)

The system Python is externally managed, so always use the project venv:

```bash
python3 -m venv .venv
source .venv/bin/activate        # re-run this in every new terminal
pip install -e ".[dev]"
```

### The loop

```bash
pytest -q                                  # must pass
ruff check .                               # must be clean
python -m alpha_engine.cli.main scan BTC   # manual end-to-end check
```

### Before committing

- Tests pass and lint is clean.
- No secret/key committed. `.env` is gitignored; only `.env.example` is tracked.
- `data/cache/` is gitignored (regenerated on run); never commit cached market data.
- If you touched the schema, bump `SCHEMA_VERSION` and update all consumers.

---

## 7. Instructions specifically for Claude Code / AI assistants

When asked to extend this project:

1. **Respect the cardinal rule (Section 2) above all else.** If a request would put
   an LLM or randomness in the decision path, flag it and propose the correct layer
   instead, rather than silently complying.
2. **New data source?** Write an adapter in `ingestion/` that outputs the normalized
   models in `cache/models.py`. Prefer keyless/free sources. Gate anything needing
   credentials behind config so the default clone still runs with zero setup.
3. **New analyzer?** Pure function from a cache model to a `SignalSource`, placed in
   `analyzers/`, with unit tests pinning behavior on fixed inputs. Follow the
   `crypto_trend.py` + `tests/test_core.py` pattern.
4. **Always add tests** for deterministic logic. A change to `analyzers/` or
   `synthesis/` without a corresponding test is incomplete.
5. **Never weaken the disclaimer or imply profit.** Describe heuristics plainly.
6. **Keep the default path keyless.** Do not introduce a hard dependency on a paid
   API or a required key into the crypto default flow.
7. **Match existing style:** type hints, `from __future__ import annotations`,
   docstrings that explain *why* not just *what*, Pydantic for all data shapes.
8. When in doubt about scope or ordering, consult `FUTURE_WORK.md` and prefer the smallest
   change that is testable on its own.

---

## 8. The one-paragraph summary (if you read nothing else)

This is a free, open, deterministic engine that turns market data into
confidence-scored research signals. Numbers come from tested pure-Python; an LLM may
only write the prose rationale and never a number. Data flows ingest -> cache ->
analyze -> synthesize -> narrate -> (soon) validate. The crypto path runs with zero
setup. It is research/education only, never advice. Keep it deterministic, keep it
honest, keep the default path keyless, and prove value with the validation layer
rather than asserting it.
