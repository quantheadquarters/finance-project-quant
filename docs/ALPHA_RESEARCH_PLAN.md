# An honest path to a demonstrated alpha

**Status (2026-09-22): candidate implemented and rejected, not a trading signal.**
This document is research only, not financial advice. No result here establishes a
repeatable edge or authorizes live trading.

## What “alpha” means here

Alpha is return above an appropriate, investable benchmark **after costs and
risk**, demonstrated on data not used to choose the rule. A profitable backtest
alone is not alpha. A rule can make money simply because the stocks it holds
rose, or because its researcher selected winners after knowing the future.

## What we built and why

The first hypothesis is slow-moving price momentum, a published phenomenon in
stocks and futures ([Jegadeesh and Titman](https://www.jstor.org/stable/2328882),
[Moskowitz, Ooi and Pedersen](https://fairmodel.econ.yale.edu/ec439/mosk.pdf)).
This is a *hypothesis*, not evidence that this particular implementation works.
Low turnover matters because trading costs can erase many reported anomalies
([Novy-Marx and Velikov](https://www.nber.org/papers/w20721)). We therefore
rebalance only monthly.

The fixed rule in `src/alpha_engine/validation/momentum_candidate.py`:

1. Use the four declared stocks AAPL, MSFT, GOOGL, NVDA. On the first trading
   close of each month, calculate each stock's return from 252 trading days ago
   to 21 trading days ago. This skips the latest month and uses only prices
   already seen at decision time.
2. Require the current close above the average of the last 200 closes. Rank
   eligible stocks by momentum; hold up to two, each at 50% of account value.
   Unused weight stays in cash. This is the **technical** version.
3. For the **technical + fundamental** version, veto a stock only if its latest
   publicly filed, fresh quarterly revenue is below the same fiscal quarter a
   year earlier. Missing or stale fundamentals do not become made-up negatives.
   Filing availability is gated by the SEC filing date plus one UTC day.
4. A decision at a close first trades at the *next* open. Positions drift
   naturally between monthly changes (no free daily rebalancing). Charge a
   per-side fee on actual weight changes and on final liquidation.
5. Compare both versions with an equal-weight four-stock buy-and-hold account
   starting on the identical date, with identical price basis and fee model.

The SEC adapter in `src/alpha_engine/ingestion/sec_fundamentals.py` now verifies
four CIK/name/revenue-tag mappings. Alphabet's historical revenue tag changed;
the adapter combines both verified tags without replacing earlier first-filed
facts. The adapter still fails loudly on a changed source contract. The rule is
under `validation/`, **not** in the live signal pipeline: a failed research
candidate cannot silently change scans. The LLM does not compute any trading
number.

To reproduce with cached data:

```bash
# SEC_USER_AGENT must identify you to the SEC, e.g. name and contact email.
./start.sh ingest AAPL MSFT GOOGL NVDA
.venv/bin/python -c 'from alpha_engine.cache.interface import Cache; from alpha_engine.ingestion.yahoo import fetch_daily; c=Cache(); [fetch_daily(a, days=1825, cache=c) for a in ("AAPL", "MSFT", "GOOGL", "NVDA")]'
.venv/bin/python -m alpha_engine.validation.momentum_candidate
.venv/bin/python -m alpha_engine.validation.momentum_candidate --cost-bps 25
```

The price cache needs about five years of daily bars for the full experiment;
an ordinary short lookback may make the command report insufficient data.
The SEC provides public filing facts; news is **not** included here because
there is no complete, timestamped, point-in-time news archive for these four
stocks. Current headlines cannot honestly be backfilled as historical news.
An LLM-written thesis may explain a future signal, but cannot vote on its
direction or confidence under this repo's design contract.

## First measured result — this candidate fails

Five-year daily price caches contain 1,253 common dates, 2021-09-23 through
2026-09-21. The first 252 bars are warmup. The account runs from 2022-09-23
through 2026-09-21, 49 monthly decision points. The AAPL holdout begins
2026-09-22 and contributes **zero** bars to this report. Figures below use
10 basis points (0.10%) per side; they omit dividends, slippage, taxes, and
market impact.

| Account | Total return | Sharpe | Max drawdown |
|---|---:|---:|---:|
| Momentum, price only | +200.89% | 1.122 | -26.30% |
| Momentum + revenue veto | +135.31% | 0.946 | -26.30% |
| Equal-weight buy and hold | +553.77% | 1.505 | -37.22% |

The revenue veto rejected six initial monthly picks and **reduced** the final
account's return. At 50 basis points per side, the technical version still
trails buy-and-hold (+180.90% versus +548.54%). That is not a reason to tune
the rule until it wins: doing so would turn this period into a training set
and overstate evidence. The four companies were selected knowing that they
survived and became large winners. This tiny, survivor-selected universe is
not a fair test of a broad stock-market claim. Nor does higher buy-and-hold
return alone settle a risk-adjusted comparison: both must be evaluated against
cash, a broad index, and matched risk in a larger, clean dataset.

## Next research program (ordered, with stop gates)

1. **Build the test dataset before proposing another rule.** Obtain licensed or
   otherwise verifiably historical US-equity daily total-return data with
   splits, dividends, delisted stocks, and dated index membership. Store source,
   original timestamp, adjustment policy, and an immutable data-version hash.
   Use SEC accession/filing timestamps for fundamentals and a genuinely dated
   news archive for sentiment. Reject a source if its historic revisions cannot
   be reconstructed. Free current-constituent lists are not enough.
2. **Write the research protocol before seeing evaluation returns.** Fix the
   universe, tradability/liquidity filters, benchmark, signal horizon, execution
   delay, fee/slippage assumptions, sector/market exposure limits, and statistical
   test. Log every attempted candidate, including failures. The two versions
   above count as attempted candidates.
3. **Research a small hypothesis set, not hundreds of knob combinations.**
   Candidate families: medium-term momentum, post-earnings-announcement drift,
   and conservative quality/earnings confirmation. Earnings surprise needs a
   point-in-time analyst-expectations archive; without it, do not label a proxy
   as true earnings surprise. News sentiment needs publication timestamps and
   entity disambiguation. Compare every addition with its immediate price-only
   baseline on the same dates. Prefer an addition only when its *incremental*
   net benefit survives costs and multiple-testing adjustment.
4. **Validate without leakage.** Use rolling, chronological train/validation
   windows and purge any overlapping forward-return labels. Tune only in the
   training windows. Run a single pre-registered final evaluation on a sealed
   holdout; never retune on that holdout. Check no-lookahead by truncating all
   prices, filings, and news at each decision date. Report each year, sector,
   market regime, turnover, drawdown, and confidence interval; use a block
   bootstrap or similarly time-dependence-aware inference and account for all
   tried hypotheses. A four-stock, four-year sample cannot meet this gate.
5. **Promotion gate.** The candidate must have positive *net* excess return
   versus a broad investable benchmark and a risk-matched benchmark, under
   realistic and doubled costs; a positive uncertainty-adjusted effect after
   accounting for candidate selection; no catastrophic concentration in one
   name or year; and no structural lookahead or data defects. If it fails any
   gate, leave it in `validation/`, record the failure, and move on. Do not turn
   a backtest winner directly into an engine weight.
6. **Forward paper record.** Freeze the code and data rules, publish a dated
   version hash, then collect next-open paper fills and outcomes for at least
   several market regimes. Compare with the same benchmarks including real
   quoted spreads. Only after an independently repeatable forward result should
   a deterministic, unit-tested analyzer be proposed for the live research
   engine. That would still be research-only, not investment advice.

“Scrape everything” is neither necessary nor reliable: it mixes duplicate,
revised, legally restricted, and future-known data. The scarce input is a
historically correct, timestamped dataset and an untouched evaluation, not
more headlines or a more confident model.
