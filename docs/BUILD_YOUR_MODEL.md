# Build and test your own market rule

Alpha Engine is a starting point for **testing ideas**, not a source of proven
trades. Its built-in signal has shown no detectable directional edge in the
published evaluation in [FINDINGS.md](../FINDINGS.md). The four-stock momentum
candidate also lost to its benchmark. Keep these results visible when sharing a
new experiment. Research and education only; not investment advice.

## First experiment, from a clean clone

```bash
./start.sh scan AAPL --days 365 --no-record
./start.sh ingest AAPL
./start.sh dashboard
```

Open <http://localhost:8000/research>, select **Try the first example**, and
compare the rule with buy-and-hold over the same dates. The page shows source
gaps and the assumptions behind the comparison. `scan --no-record` fills the
price cache without adding a development result to the permanent signal log.
News, SEC filings, and FRED macro context are optional; set their variables in
your ignored `.env` and run `ingest` when available. The strategy experiment
uses prices only, so missing context does not become an invented input.

For a network-free tutorial, after `./start.sh` has installed the package, run:

```bash
.venv/bin/python scripts/load_demo.py
./start.sh dashboard
```

Choose **Use offline demo** in `/research`. `DEMO` is 260 **synthetic** daily
candles with no real market claims. It is safe to share; cached Apple data and
API responses are not bundled with the repository.

## What the downloaded experiment contains

The **Download reproducible result** button saves JSON with the exact candles
used, their SHA-256 fingerprint, provider and currency if known, adjustment
policy, fetch date, engine version, strategy file hash, parameters, capital,
costs, trades, lookahead check, equity curve, and both comparisons. Re-running
the same strategy code on those candles and settings should reproduce the
result. A changed source hash or price fingerprint means the run is different.

The buy-and-hold comparison enters at bar 1's open, when a close-of-bar rule
could first trade, and pays the same entry cost. The cash-blended comparison
uses the strategy's *full-period realized volatility* to choose a fixed stock
fraction. It is a hindsight diagnostic, not a tradeable benchmark; it omits
rebalancing costs and cannot match a strategy more volatile than unlevered
buy-and-hold. A lookahead warning voids the strategy metrics, no matter how
good they look.

## Three extension paths

### 1. Change a trading rule

Copy [the SMA example](../src/alpha_engine/strategy/builtin/sma_crossover.py)
into `strategies/my_rule.py`. Rename its class, then change
`generate_signals(candles)` to return `1`, `-1`, or `0` for each bar. Use the
aligned helpers in `strategy/indicators.py`; they keep indicator output on the
same bar index as prices. Run:

```bash
./start.sh strategies
./start.sh strategy-backtest AAPL --strategy MyRule --chart my-rule.html
```

The file stays local. The web API accepts a strategy name and parameters,
never Python source code. A local strategy can execute arbitrary Python, so
only install code you trust.

### 2. Test a factor

A factor is one number computed from information available at bar `t`. Follow
the `_add(...)` patterns in [the factor registry](../src/alpha_engine/quant/factors.py);
the registry tests reject reading past
`t`. Then run `./start.sh factors AAPL`. Compare the factor's information
coefficient (rank correlation with later returns) to the **noise floor**: with
hundreds of trials, a high rank can occur by chance. Do not add it to the
engine just because it ranks well on one stock.

### 3. Add a data provider

Write an adapter in `ingestion/` that turns a source response into the shapes
in `cache/models.py`, records per-feed health and item count, and leaves scans
cache-only for news and fundamentals. Keep API keys optional. Preserve source,
currency, public availability date, and adjustment policy when the provider
can supply them. Add a network-free parser fixture test. See
`ingestion/yahoo.py` and `ingestion/sec_fundamentals.py` for examples.

## Before calling a result “alpha”

Save the rule and parameters before testing a holdout. Use a universe with
historical membership and delisted stocks; verify splits, dividends, filing
availability, and costs. Compare with a broad-market and risk-matched benchmark
on the same dates. The current free Apple cache cannot establish those claims.
See [the research plan](ALPHA_RESEARCH_PLAN.md) for the promotion gates.
