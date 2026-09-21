# Alpha Engine: Beginner's Guide

This guide explains the whole repository without assuming that you already know
Python, quantitative finance, or market-data systems. For the exact engineering
rules, read `context.md`; for a shorter product explanation, read
`HOW_IT_WORKS.md`.

> **Research only.** This software is not financial advice and its current
> signals have no demonstrated predictive edge.

## The idea in one minute

Alpha Engine collects market facts, stores them locally, turns each kind of fact
into a small opinion, and combines those opinions into one research signal.

```text
internet -> local cache -> analyzers -> weighted vote -> signal -> backtest
```

For Apple, that means:

- **Technical data:** open, high, low, close, and volume candles. Technical
  analyzers inspect trend, momentum, volatility, support/resistance, and volume.
- **Fundamental data:** revenue, profit, margins, debt, cash flow, and valuation.
  These can confirm or challenge the price-based view.
- **News:** company-tagged headlines. A deterministic word list measures whether
  recent coverage is broadly positive or negative.
- **Macro data:** rates, inflation, and unemployment. These provide context, not
  a primary trading instruction.

Technical evidence deliberately has the largest influence. Fundamentals and
news have capped weights, so they can support or challenge the price evidence
without silently taking over the decision.

## Important words

- **Candle:** prices for one time interval: open, high, low, close, and usually
  volume.
- **Signal:** the engine's structured research view: long, short, or neutral,
  plus confidence, evidence, and an invalidation level.
- **Alpha:** performance beyond an appropriate benchmark after realistic costs.
  A profitable backtest alone is not proof of alpha.
- **Analyzer:** a deterministic function that converts one data type into a
  small directional vote.
- **Weight:** how much one analyzer's vote contributes. It is not position size.
- **Backtest:** a replay of a rule on historical data.
- **Lookahead:** accidentally using information that was not available at the
  simulated decision time. It makes results invalid.
- **Sharpe ratio:** return divided by variability, annualized. It is a comparison
  tool, not a guarantee.
- **Drawdown:** the decline from a previous account peak.
- **IC (information coefficient):** rank correlation between a factor score and
  a later return. A high in-sample IC can occur by chance.

## Install and run

The easiest path is the project launcher:

```bash
./start.sh doctor
./start.sh scan AAPL --no-record
./start.sh report AAPL
./start.sh backtest AAPL --no-refresh
```

`--no-record` keeps a developer check out of the permanent signal track record.
`--no-refresh` makes a test use only local cached data.

Optional context sources need free API credentials in `.env`:

```text
FINNHUB_API_KEY=...   # company-tagged news
FMP_API_KEY=...       # company fundamentals
SEC_USER_AGENT=Name email@example.com  # SEC filing headlines
LLM_API_KEY=...       # prose only; never signal numbers
```

Then refresh cache-only context and scan:

```bash
./start.sh ingest AAPL
./start.sh health
./start.sh scan AAPL --no-record
./start.sh scan AAPL --no-record --llm
```

The scan itself does not fetch news or fundamentals. `ingest` does that first,
then scans read the cache. This keeps scans fast, avoids API rate-limit bursts,
and makes tests network-free.

## What each main folder does

| Path | Plain-English purpose |
|---|---|
| `src/alpha_engine/ingestion/` | Talks to outside data providers and normalizes their responses. This is the network boundary. |
| `src/alpha_engine/cache/` | Defines stored data shapes and reads/writes the local cache. |
| `src/alpha_engine/analyzers/` | Pure Python opinions such as trend, RSI, fundamentals, and sentiment. No network and no randomness. |
| `src/alpha_engine/synthesis/` | Combines analyzer votes and calculates confidence. |
| `src/alpha_engine/narrative/` | Writes only the human-readable thesis. The optional LLM is confined here. |
| `src/alpha_engine/schema/` | Defines the final `Signal` contract used everywhere else. |
| `src/alpha_engine/validation/` | Records signals, scores outcomes, and replays the engine without future data. |
| `src/alpha_engine/quant/` | Factor research, statistical reports, regime models, and option pricing. |
| `src/alpha_engine/strategy/` | Runs user-authored trade rules, metrics, and chart reports. |
| `src/alpha_engine/execution/` | Paper-first order plumbing. Live trading requires an explicit gate. |
| `src/alpha_engine/orchestrator/` | Coordinates ingestion and targeted rescans. |
| `src/alpha_engine/web/` | Dashboard, REST API, and HTTP MCP transport. |
| `src/alpha_engine/toolkit.py` | One shared tool table used by MCP, HTTP, and the AI terminal. |
| `tests/` | Network-free checks that pin financial, security, and no-lookahead behavior. |

## Follow one Apple scan through the code

1. `cli/main.py` recognizes AAPL as a US equity.
2. The price loader refreshes daily candles when allowed, then stores them.
3. `_build_price_signal` calls the technical analyzers on those candles.
4. It reads cached fundamentals, news, and macro observations. Missing optional
   data causes an abstention, not invented values.
5. `synthesis/synthesize.py` combines the votes. The volatility and calendar
   layers may only reduce confidence.
6. `schema/signal.py` validates the completed numeric signal.
7. `narrative/narrator.py` writes a template thesis or asks an LLM to rephrase
   it. Only the `thesis` field can change.
8. Unless `--no-record` was used, the signal is appended to the validation log.

This order matters: the LLM receives an already-complete signal and cannot
change direction, confidence, source weights, or invalidation price.

## The two kinds of backtest

They answer different questions:

| Command | Question |
|---|---|
| `backtest AAPL` | Were the full engine's historical signals directionally right? |
| `strategy-backtest AAPL --strategy NAME` | What would one explicit trading rule have done to an account? |

The engine backtest truncates prices and every context source at the simulated
date. Fundamentals use their publication timestamp, not the quarter-end date.
An older cached fundamental record without a publication timestamp abstains,
because treating quarter end as public knowledge would leak an earnings report.

The strategy backtester detects lookahead by rerunning the strategy on truncated
history. If it reports any `lookahead_violations`, every return and risk metric in
that report is void. Signals fill one bar later so a strategy cannot trade the
same closing price it used to make its decision.

## The five built-in strategy candidates

```bash
./start.sh strategies
```

- `SMACrossover`: follows a short moving average crossing a long average.
- `RSIReversal`: looks for an overextended move to reverse.
- `SupertrendFlip`: enters when Supertrend changes direction.
- `DonchianBreakout`: enters beyond the previous price range.
- `MomentumVolume`: follows a medium-term move only when volume confirms it.

These are deliberately small, readable research candidates. They are not five
proven alphas. To create your own, copy the pattern in `strategies/README.md`.

Create an interactive visual report:

```bash
./start.sh strategy-backtest AAPL --strategy SMACrossover \
  --cost-bps 10 --chart aapl-sma.html
```

The HTML embeds the candles, signals, and equity data. It loads the pinned
TradingView Lightweight Charts display library from a CDN when opened.

## Hardcoded model versus LLM version

The safe comparison is:

```bash
./start.sh scan AAPL --no-record
./start.sh scan AAPL --no-record --llm
```

The two outputs must have identical direction, confidence, invalidation level,
and analyzer weights. Only the thesis wording may differ. An LLM-selected trade
cannot be replayed deterministically and would violate the project's most
important safety rule, so this repository intentionally does not offer one.

## How to judge a result honestly

Before calling anything alpha, ask:

1. Did it beat a suitable benchmark after transaction costs?
2. Was every input available at that historical time?
3. Were parameters chosen before viewing the test period?
4. Does it survive different costs, nearby parameters, and different periods?
5. Are there enough independent trades to distinguish skill from luck?
6. For factors, did the score beat the multiple-testing noise floor?

If any answer is no, the right label is **candidate**, not alpha.

## Safe ways to extend the project

- Add market data in `ingestion/`, normalize it into `cache/models.py`, record
  source health, and make failures explicit.
- Add an analyzer as a pure function in `analyzers/`, then wire it into the live
  signal path and add fixed-input tests.
- Add a factor with one registry entry in `quant/factors.py`; the registry-wide
  lookahead check covers it automatically.
- Add a local strategy by subclassing `BaseStrategy`; no registration is needed.
- Add an outside tool only through `toolkit.py`, which keeps all three API
  surfaces synchronized.

Never place network access in an analyzer, let an LLM set a number, accept Python
strategy code over HTTP, rewrite an old signal-log line, or disable TLS checking.

## Verification before calling a change complete

```bash
pytest -q
ruff check .
ruff format --check .
python -m alpha_engine.cli.main scan BTC --no-record
```

The tests are intentionally network-free. The final scan is the manual
end-to-end check.

## Troubleshooting map

- Run `./start.sh doctor` first.
- Run `./start.sh health` when a context source appears empty.
- No fundamentals or company news usually means the optional keys are absent or
  `ingest` has not run.
- A certificate error across many providers usually means the local CA trust
  store is broken; never fix that by disabling certificate verification.
- A strategy with excellent numbers and lookahead violations has no valid
  numbers.
- The source of truth for current limitations is `FINDINGS.md`; planned work is
  in `FUTURE_WORK.md`.

