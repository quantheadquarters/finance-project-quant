# How It Works

This document explains the engine twice.

**Part 1** assumes you know nothing about finance or programming. No jargon
without a definition, no code.

**Part 2** is the technical version: the actual modules, data shapes, and
algorithms.

Read Part 1 even if you are technical — it explains *why* the architecture looks
the way it does, and that reasoning is the interesting part.

---

# Part 1 — The plain-language version

## What is this thing?

It is a program that looks at the price history of something you can trade — a
stock like Apple, a cryptocurrency like Bitcoin — and produces a written opinion
that looks like this:

> **BTC — bullish, 52% confidence.** Thesis wrong below $91,400.
> Contributing reads: trend (bullish), RSI (neutral), volume (bullish),
> funding rate (bearish), news sentiment (neutral).

Three things are worth noticing about that output.

**It shows its work.** You get every individual opinion that fed the final
answer, and how much each one counted. Nothing is hidden inside a black box.

**It admits doubt.** "52% confidence" is barely more than a coin flip, and the
engine says so rather than dressing it up. Confidence above 75% is not
reachable by design.

**It says how it could be wrong.** The *invalidation level* is the price at
which the reasoning stops making sense. A view you cannot disprove is not a
view, it is a mood.

## The one rule that shapes everything

> **Every number is calculated by ordinary, tested code. An AI model is allowed
> to write the English paragraph, and is never allowed to touch a number.**

This is the most important sentence in the project, so here is why it matters.

Suppose you let ChatGPT decide the confidence score. Ask it the same question
tomorrow and it might say 61% instead of 52% — language models are not
guaranteed to repeat themselves. Now try to check whether the engine was any
good last year. You cannot. You would have to re-ask the model about every past
day, and it would give different answers than it gave then. Your entire track
record becomes unverifiable.

The word for "same input always produces the same output" is **deterministic**.
Determinism is what makes it possible to replay history and honestly ask *did
this ever work?*

So: the maths is plain Python. The AI, if you enable it at all, only rewrites
the summary paragraph into nicer prose. It is optional, off by default, and
cannot change a single digit.

## How a signal gets made, in five steps

Think of it as an assembly line. Each station does one job and hands its work to
the next. Work only ever flows one direction.

### Step 1 — Fetch the data

Code called **adapters** go out to the internet and collect raw information:
price history from CoinGecko, company filings from the SEC, interest rates from
the US Federal Reserve, headlines from RSS feeds.

Every source formats its data differently. Each adapter's job is to translate
its source into one shared standard shape, so nothing downstream has to know or
care where a number came from.

### Step 2 — Store it locally

Everything fetched gets saved to your own computer, in the `data/cache/` folder.

Two reasons. First, speed — reading a local file beats a network request every
time. Second, politeness: free data services will block you if you request the
same thing a hundred times an hour.

Each kind of data has a shelf life, called a **TTL** (time to live). A live
price goes stale in minutes. An inflation statistic published monthly is good
for a day. When data passes its shelf life the engine knows to fetch a fresh
copy — and, critically, it never quietly serves you something rotten while
pretending it is current.

### Step 3 — Analyze it

This is where the opinions come from. An **analyzer** is a small, focused piece
of code that looks at the data and answers exactly one question.

- The trend analyzer asks: *is the price generally going up or down?*
- The RSI analyzer asks: *has this moved so far so fast that it is due a pause?*
- The volume analyzer asks: *are lots of people trading this, or is it drifting
  on thin activity?*
- The funding analyzer asks: *are speculators crowded on one side of this trade?*
- The news analyzer asks: *have recent headlines been positive or negative?*

There are more than twenty of these. Each returns two things: a direction
(bullish, bearish, or neutral) and a **weight** between 0 and 1 saying how
strongly it feels.

Analyzers are strictly independent. None of them can see what the others
concluded, so none can be talked into agreement. If they all say "bullish", that
is genuine corroboration rather than an echo.

An analyzer that lacks the data it needs returns weight zero — it abstains. It
never guesses. This turns out to matter enormously: a guessed number and a real
number look identical once they are in a spreadsheet.

### Step 4 — Combine the opinions

**Synthesis** takes all those separate votes and folds them into one answer.

It is a weighted vote, not a simple majority. Then confidence is calculated from
three things:

1. **Agreement** — what share of the total weight points the same way? Analyzers
   split down the middle should produce low confidence, even though the vote
   still has a winner.
2. **Reliability** — how accurate has each analyzer been historically? This is
   measured from real recorded outcomes, not assumed. An analyzer with a poor
   record has its vote quietly discounted.
3. **Diversity** — how many independent analyzers contributed? One analyzer,
   however sure, is capped at 45% confidence. It takes several agreeing to
   climb higher.

The ceiling is 78%. Nothing here is ever certain, and the number should not be
able to say otherwise.

### Step 5 — Write it down, and check it later

Every signal gets appended to a permanent log file. Nothing is ever edited or
deleted — you cannot quietly rewrite a bad call after the fact.

Later, the engine goes back, looks up what the price actually did, and scores
each past signal. That produces the honest answer to *does this work?*

Right now that answer is: **roughly a coin flip.** The analyzers are honest
scaffolding, not proven money-makers. The project says so everywhere rather than
hiding it — the machinery for finding out is the point, and it works.

## Two other things the engine does

### Ranking factors

A **factor** is any single number you can compute from price history that might
predict what happens next. "How much did this move in the last 20 days" is a
factor. So is "how far is the price from its 50-day average".

The engine has **504 of them**, and it can measure how well each one actually
predicted the future for a given asset.

This comes with a trap the engine protects you from. Test 500 random factors and
some will look brilliant purely by luck — in the same way that if 500 people
flip a coin ten times, someone gets ten heads and looks gifted.

So the engine computes a **noise floor**: the score the luckiest of 500
*completely useless* factors would have reached by chance. If your best factor
does not clear that line, you have learned nothing, and the engine tells you so
in those words.

### Knowing when to be less sure

The engine scrapes the Federal Reserve's meeting calendar, so it knows when the
next US interest-rate decision is. In the days before one, it lowers its own
confidence — not because it predicts the outcome, but because it knows a number
it cannot see is about to move the market.

This only ever reduces confidence, never raises it. Dates it cannot scrape (the
RBI's page publishes none in readable form) go in a `calendar.json` file you
control.

### Reacting to news

The engine reads announcement feeds from stock exchanges and central banks. When
a headline mentions a company you follow, it schedules a fresh look at just that
company — rather than re-analyzing your whole portfolio because one company had
news.

Headlines are scored by counting known positive and negative finance words
("beats", "surges" versus "plunges", "fraud"), handling negation ("*not*
profitable" is bad news), and fading with age. A three-week-old headline is
history, not news.

That scoring is a dictionary and a counter — deliberately not an AI — because it
produces a weight, and weights are numbers.

## What it deliberately does not do

- **It does not place trades.** There is order-placement code, it defaults to
  paper trading, and it requires explicit environment variables to go live.
- **It does not predict prices.** It describes conditions and states a
  directional lean with honest uncertainty.
- **It does not value companies.** Discounted cash flow models need assumptions
  you invent — a discount rate, a growth rate — and a number you invented does
  not become a fact by being in a spreadsheet.
- **It does not use machine learning.** Not out of principle, but because
  earning it requires a year of real recorded outcomes and a genuinely untouched
  test period. Training a model before then produces prettier charts and a worse
  track record.

---

# Part 2 — The technical version

## Layer architecture

One-way pipeline. Each stage is a package under `src/alpha_engine/`, and each
may only import from stages to its left.

```text
ingestion/ → cache/ → analyzers/ → synthesis/ → narrative/ → Signal → validation/
(network)   (local)   (pure fns)  (weighted    (prose only,          (append-only
                                   vote)        key-gated)            log, backtest)
```

The direction of that arrow is enforced socially, not mechanically, but every
violation is a bug. An analyzer that imports from `ingestion/` has reached across
the cache boundary and can now make a network call inside the decision path.

### `schema/signal.py`

The contract every layer compiles against.

```python
class Signal(BaseModel):
    asset: str
    market: Market
    direction: Direction          # bullish | bearish | neutral
    confidence: float             # [0, 1], calibrated
    timeframe: Timeframe
    signal_sources: list[SignalSource]   # the full audit trail
    invalidation_level: float | None
    thesis: str                   # the ONLY LLM-writable field
    timestamp: datetime           # tz-aware UTC, enforced
```

Changing a field means bumping `SCHEMA_VERSION` and updating every consumer.

### `cache/`

`models.py` holds the normalized shapes: `PriceSeries`/`Candle`, `OptionsChain`,
`MacroObservation`, and the Phase 11 additions `NewsItem`, `OnChainObservation`,
`Fundamentals`, `EventItem`.

`interface.py` provides `Cache`, backed by `LocalStore` (JSON files under
`data/cache/`). Every kind carries a TTL; getters return `(data, stale)` so the
caller decides whether to refresh. The cache never silently serves rot.

The four Phase-11 collections share one generic merge-by-key implementation
rather than four near-identical ones. Bucket names are sanitized into
filesystem-safe strings, so a source id can never escape the cache directory.

### `analyzers/`

Pure functions: normalized data in, `SignalSource` out. No network, no
randomness, no clock beyond an injectable `now`.

| Analyzer | Reads | Question |
|---|---|---|
| `crypto_trend` / `indian_equity` / `forex_trend` | `PriceSeries` | core directional read |
| `rsi`, `macd`, `bollinger` | `PriceSeries` | momentum, stretch |
| `volume`, `vwap` | `PriceSeries` | participation |
| `support_resistance`, `multi_timeframe` | `PriceSeries` | structure |
| `volatility` | `PriceSeries` | regime (also emits a global scalar) |
| `macro_context` | `MacroObservation` | policy posture, region-aware |
| `sentiment` | `NewsItem` | lexicon score with decay |
| `crypto_onchain` | `OnChainObservation` | positioning |
| `fundamentals` | `Fundamentals` | accruals, leverage, growth |
| `forex_carry` | `PriceSeries` + rates | rate differential, dollar cycle |
| `macro_calendar` | `EventItem` | **scalar only** — dampens, never directs |
| `fno_oi` | `OptionsChain` | PCR, max pain, OI walls |
| `risk`, `correlation`, `portfolio_signal` | multiple | portfolio context |

Two analyzers return scalars rather than `SignalSource` objects:
`volatility_scalar` and `calendar_scalar`. Both are bounded to `(0, 1]` — they
can only ever reduce conviction. A "defensive" mechanism that could raise
confidence would be a bug wearing a costume.

**How they apply, and the bug that shaped it.** These scalars used to work by
multiplying every source weight by a constant. That does nothing. Every term in
`_calibrate_confidence` is a ratio — agreement is `agreeing/total`, reliability
is a weighted mean, `net` is `score/total` — so a constant factor cancels out of
all of them, and a scan in a violent tape produced a dampened-looking audit
trail with a bit-identical confidence number.

They now do both: source weights are still scaled, so a reader of the audit
trail sees the inputs were discounted, and the product of the scalars is passed
to `synthesize(conviction_scalar=...)`, which applies it to the final confidence
where it cannot cancel. The weights are the explanation; the scalar is the
effect.

The backtester applies the volatility scalar identically, so it measures the
same engine the live path runs. It deliberately does *not* apply the calendar
scalar — replaying history against today's calendar would be lookahead.

### `synthesis/synthesize.py`

```python
net_direction  = weighted vote over sources          → direction, net ∈ [-1, 1]
agreement      = agreeing_weight / total_weight
reliability    = weighted mean of SOURCE_RELIABILITY (calibrated from outcomes)
raw            = 2 / (1 + exp(-4 · agreement · reliability · |net|)) - 1
confidence     = raw · source_count_cap
```

`source_count_cap` = `{1: 0.45, 2: 0.60, 3: 0.70, 4: 0.75, 5+: 0.78}`.

`SOURCE_RELIABILITY` starts at conservative defaults and is overwritten at import
time from `data/calibration.json` when `calibrate` has been run — reliability is
measured from recorded outcomes with shrinkage toward the prior, not assumed.

### `quant/factors.py` — the factor registry (Phase 10)

504 factors across ten families, generated by systematic parameterization.

```python
@dataclass(frozen=True, slots=True)
class FactorSpec:
    name: str
    family: str
    min_bars: int
    fn: Callable[[Bars, int], float | None]
    cost: str          # "fast" | "slow"
```

Three design decisions worth explaining:

**Column-oriented input.** Factors receive a `Bars` object (parallel lists of
open/high/low/close/volume) plus an index `t`, rather than pydantic `Candle`
objects. Constructing models per bar per factor dominated every other cost; a
panel is 500 factors × hundreds of bars.

**Lookahead is pinned, not assumed.** `fn(bars, t)` may read only indices
`[0..t]`. `tests/test_factors.py` parameterizes over the *entire registry* and
asserts each factor returns an identical value on a series truncated at `t`. Add
a peeking factor and that test fails automatically.

**Cost tiers.** GARCH and HMM fit a model per bar and are ~100× everything else.
They are tagged `cost="slow"` and excluded from the default panel; `--all-factors`
opts in. This keeps `factors BTC` at ~4 seconds instead of minutes.

Coverage honesty: every factor declares `min_bars` and returns `None` before it,
so the `coverage` column means something.

### `quant/ranking.py` — measuring predictive power

- `rank_ic` — Spearman correlation between factor value at `t` and forward
  return from `t`. Rank-based, so fat tails and outliers do not dominate.
- `ic_decay` — IC at horizons 1, 5, 10, 20 bars.
- `noise_floor_ic(n_factors, n_obs)` — `sqrt(2·ln k) / sqrt(n)`, the |IC| the
  best of `k` useless factors reaches by chance. **This is the multiple-testing
  correction the whole ranking layer needs.** 500 factors against 30
  observations will produce |IC| ≈ 0.6 from pure noise. The CLI prints the floor
  next to the results and states plainly when the top factor fails to clear it.

### `orchestrator/` — batch runner and event engine (Phase 12)

`__init__.py` is the original batch runner: a fault-isolated loop over
`portfolio.json`.

`engine.py` is the event-driven layer:

- **`Trigger`** — why work exists (`scheduled`, `new_data`, `news_event`,
  `user_query`), which assets it touches, and its priority.
- **`TriggerQueue`** — a heap ordered by `(priority, insertion_counter)`. The
  counter is what makes ties deterministic; ordering by object identity would
  make runs unreproducible.
- **`SharedContext`** — per-run memo so one run fetches each series once.
  Deliberately per-run: a process-level cache would serve yesterday's prices to
  today's scan, which is exactly what TTLs exist to prevent.
- **`stale_kinds` / `refresh_context`** — freshness-driven ingestion. Asks the
  cache what has rotted and refreshes only that, with per-source failure
  isolation.
- **`triggers_from_news`** — recent tagged headlines become targeted per-asset
  re-scans. A headline about one company does not re-scan the portfolio.

### `validation/`

- `recorder.py` — append-only JSONL. No code path rewrites a line.
- `outcomes.py` — scores recorded signals against realized prices.
- `backtest.py` — `signal_at()` is the single no-lookahead truncation choke
  point, pinned by a byte-identical-output test.
- `calibrate.py` — derives `SOURCE_RELIABILITY` from outcomes with shrinkage
  toward the prior, so a lucky 5-sample analyzer does not get promoted.
- `options_backtest.py` — model-priced ATM option legs via Black-Scholes.
- `compare_context.py` — offline, same-date comparison of technical signals
  with dated fundamentals and optional historical news.
- `trade_experiment.py` — next-open, costed account replay of those signal
  modes against same-window buy-and-hold; research only, not proven alpha.

### The read-only rule for context data

Price and macro refresh inline during a scan, because a scan without a price
series is meaningless. The Phase 11 context sources — news, on-chain,
fundamentals — are **read-only in the scan path**. They are populated by
`ingest` or the orchestrator's freshness pass.

This is `cache/interface.py`'s own stated rule ("analyzers read from HERE, never
from the network; an ingestion service keeps the store fresh"). Fetching four RSS
feeds and three APIs on every `scan` would turn a sub-second command into a
multi-second one, rate-limit free APIs, and make the test suite hit the network.

## Determinism guarantees, and how each is enforced

| Guarantee | Enforcement |
|---|---|
| No lookahead in factors | Registry-wide parameterized truncation test |
| No lookahead in backtests | Single `signal_at` choke point + byte-identical pin |
| No network in analyzers | Analyzers take data, never a `Cache` or client |
| No randomness anywhere decisional | No `random`/`time` in `analyzers/` or `synthesis/` |
| LLM writes no numbers | LLM confined to `narrative/`, writes only `thesis` |
| Log is immutable | `recorder.py` appends only |
| Tests never hit the network | Whole suite runs offline; ~23s |

## Testing

~2100 tests, all network-free. The parameterized factor tests dominate the count
(three assertions × 504 factors).

The tests that matter most are the ones pinning *inverted* or *defensive* logic,
because those are what get silently flipped during a refactor and then quietly
lose money:

- funding rate is contrarian (crowded longs → bearish)
- a strong dollar lifts USDINR but pushes EURUSD down
- the calendar scalar can only ever reduce confidence
- NSE/RBI scrapers fail **loudly** — an unrecognized page shape prints
  `CONTRACT BROKEN` and returns nothing, rather than a plausible wrong number

That last one deserves emphasis. Scraped sources break silently by nature. A
scraper that returns confident garbage is far more dangerous than one that
returns an error, because a weight computed from garbage still looks like a
weight.

## Where to change things

| Task | Where |
|---|---|
| Add a data source | `ingestion/<name>.py` → emit `cache/models.py` shapes |
| Add an analyzer | `analyzers/<name>.py` → pure fn → `SignalSource`; wire in `_build_price_signal` |
| Add a factor | One `_add(...)` line in `quant/factors.py` — nothing else |
| Change confidence maths | `synthesis/synthesize.py` |
| Add a CLI command | `cli/main.py`: `cmd_*` + a `sub.add_parser` block |
| Expose a tool to AI assistants | `toolkit.py`: add to `TOOLS` and `HANDLERS`. It appears on MCP-over-stdio, MCP-over-HTTP and REST at once |

Adding a factor really is one line. The moment it lands in `FACTOR_REGISTRY` it
appears in `factors` output, gets IC-scored, and is covered by the lookahead
test — with no other change anywhere. That property is the entire reason the
registry exists instead of a large function.
