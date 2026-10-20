# Research workbench: Apple first

The product goal is to give people the evidence and tools to build their own
market models. Alpha Engine's built-in signal is one documented baseline, not a
forecast to copy. **Research and education only; not investment advice.**

## What is already in this repo

| Part | Where | What a user can check |
| --- | --- | --- |
| Data collection | `ingestion/` and `cache/` | Daily candles, SEC filings, tagged news, FRED macro, and source health. Network access is confined to ingestion. |
| Alpha Engine | `analyzers/`, `synthesis/`, `narrative/` | Deterministic technical and contextual votes. An optional LLM rewrites only the thesis prose; it cannot set a decision number. |
| Signal validation | `validation/` | Recorded outcomes, no-lookahead replay, factor rankings against a noise floor. |
| User backtester | `strategy/` | A user's local `BaseStrategy` rules, next-bar fills, trade costs, equity curve, and lookahead warnings. |
| Visual checks | `strategy/chart.py`, `/research` | TradingView Lightweight Charts in an exported report; a browser candlestick and equity view in the workbench. |
| Interfaces | `toolkit.py`, `web/`, `mcp.py` | The same read-only research tools over HTTP, MCP, and the optional AI terminal. |

OpenAlgo is a useful reference for a self-hosted desk with charting, strategy
building, backtests and options analytics: <https://openalgo.in/>. Its separate
SDK and tool repositories are an ecosystem pattern. Alpha Engine can keep one
repo until independent users or release cycles justify a split. Its current
Python backtester and tool table already provide clear seams; splitting them
now would duplicate cache, schema, and validation rules. OpenAlgo's AGPL code
is a reference, not copied into this project.

[Mastertrust's mt Quant](https://mastertrust.co.in/mtquant) is another reference:
it emphasizes historical strategy testing, risk reports, comparisons, options
legs and a no-code workflow. Those are useful interface ideas, but its broker
and options execution context is different from an Apple-first research tool.
The first reusable part here is inspectable evidence plus local Python rules;
a public no-code builder would need a safe, limited rule language and a sandbox.

## The first usable workflow

1. Run `./start.sh scan AAPL --days 365 --no-record` to cache daily prices without adding a
   development scan to the permanent track record.
2. Run `./start.sh ingest AAPL` to refresh available context feeds. Set
   `SEC_USER_AGENT` first for Apple's SEC filings. FRED macro context needs
   `FRED_API_KEY`; company news needs a separate `FINNHUB_API_KEY` or another
   dated source. Missing inputs remain visible as missing.
3. Run `./start.sh dashboard` and open `/research`. Inspect the dates, price
   chart, filings, company headlines, macro coverage and the latest recorded
   signal. The setup panel names missing inputs. The page reads only the local
   cache and makes no upstream request. Run `.venv/bin/python scripts/load_demo.py`
   and select DEMO for synthetic, network-free practice.
4. Select one of the five built-in teaching rules: SMA crossover, RSI reversal,
   Donchian breakout, momentum plus volume, or Supertrend flip. Change parameters
   and replay it against buy-and-hold on matched bars and costs. The risk-matched
   cash blend is hindsight-only. Download the run's inputs and result as JSON.
   A non-empty lookahead warning voids every metric.
5. For a custom rule, put a `BaseStrategy` subclass in `strategies/`, then run
   `./start.sh strategy-backtest AAPL --strategy YourClass --chart aapl.html`.
   Opening the generated report shows the fills alongside candles with
   TradingView Lightweight Charts. The web API accepts a strategy *name*, never
   source code.

The options illustration on `/research` uses Black-Scholes prices derived from
Apple shares, **not actual historical option quotes**. It can teach leverage and
time decay, but cannot validate an executable Apple options strategy. Real
options research needs dated chains with bid, ask, implied volatility, volume,
open interest and corporate-action handling.

## Five hypotheses, not five proven alphas

The built-in rules are examples to edit and falsify. Five names in a menu do
not establish five independent sources of excess return. The existing
four-stock monthly momentum experiment also failed its equal-weight benchmark:
price-only +200.89%, SEC-confirmed +135.31%, buy-and-hold +553.77% on the same
period. See [the measured findings](../FINDINGS.md) and
[the research plan](ALPHA_RESEARCH_PLAN.md). No candidate is promoted into the
live engine on that evidence.

The next validity gate is a historical universe with delisted stocks,
historical membership, splits, dividends and delisting returns. Run the *frozen*
rule there with a broad-market and risk-matched benchmark, realistic costs and
a sealed holdout. A dated historical news archive is needed before claiming
that headline sentiment improves results. Until then, the workbench is a way
to inspect evidence and test ideas, not a predictor with demonstrated edge.

## Known gaps to address in order

1. **Provenance:** `PriceSeries` has fetch time but no provider, quote currency
   or adjustment policy. The workbench therefore labels candles as cached daily
   prices, and cannot claim a verified total-return history.
2. **Fair comparisons:** the interactive strategy result lacks a matched
   buy-and-hold and risk-matched benchmark. Treat its return as descriptive,
   never as alpha.
3. **Historical news:** the normal cache keeps 30 days of headlines. It cannot
   backfill a point-in-time multi-year sentiment test.
4. **Real options:** model-priced illustrations need historical option quotes
   before executable conclusions are possible.
5. **Public strategy creation:** loading arbitrary Python from a web request
   would execute untrusted code. Local strategy files are the supported
   extension point until a separately isolated sandbox exists.

Access setup: copy `.env.example` to `.env` and fill only the variables you
have. `FRED_API_KEY` is the existing macro key. `SEC_USER_AGENT` is a name and
contact email for SEC requests, not a secret. Do not paste keys into chat or
commit `.env`.
