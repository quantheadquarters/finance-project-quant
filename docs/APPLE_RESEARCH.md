# Apple Research Model: Design and Measured Result

This note records the AAPL investigation requested in September 2026. It
separates what was built from what the available data can honestly prove.

> **Research only. Not financial advice.** No candidate tested here demonstrated
> alpha over buying and holding Apple during the measured period.

## What “technical first, fundamentals confirm” means

The existing signal pipeline already provides the correct structure:

```text
price candles -> technical analyzers -------------------\
fundamentals -> capped confirming vote ------------------+-> deterministic signal
company news -> capped deterministic sentiment vote ----/
optional LLM -> thesis wording only, after the signal is frozen
```

Technical evidence remains primary: the price/volume votes establish the
direction first. Context may confirm that direction or veto it to neutral, but
cannot create or reverse a trade on its own. The fundamental analyzer has a
maximum weight of 0.35 and sentiment a maximum of 0.30. Missing context abstains;
it never becomes a made-up neutral or a guessed value.

The fundamental analyzer scores operating-cash-flow quality relative to net
income, leverage, and matching-quarter year-over-year revenue growth. Gross
margin is stored for inspection but is not currently a vote. The sentiment
analyzer uses a transparent positive/negative finance lexicon, ticker tags,
and recency decay. Neither uses an LLM or makes a network call.

## The hardcoded and LLM comparison

There are two presentation modes, but only one numeric model:

1. **Deterministic version:** Python calculates every number and a fixed template
   explains it.
2. **LLM-narrated version:** the exact same frozen numbers are sent to an LLM,
   which may rewrite only the thesis prose.

That is the valid comparison for this repository. Letting the LLM choose a
direction or confidence would make the result non-repeatable, difficult to
backtest, and contrary to the engine's cardinal rule. Tests verify that enabling
the narrator cannot change numeric fields.

Use:

```bash
./start.sh ingest AAPL
./start.sh scan AAPL --no-record
./start.sh scan AAPL --no-record --llm
```

`SEC_USER_AGENT` (your name and contact email, not an API key) now enables
Apple fundamentals from the [SEC company-facts API](https://www.sec.gov/search-filings/edgar-application-programming-interfaces).
`FMP_API_KEY` remains an optional alternative for other equities;
`FINNHUB_API_KEY` supplies independent company news; `LLM_API_KEY` enables the
prose rewrite. Without a key, the LLM mode falls back to the template. Its
numeric fields were checked against the deterministic mode and matched exactly;
an actual LLM response could not be tested because no LLM key was configured.

The live SEC ingestion normalized quarterly rows, including Q4 values derived
from filed annual minus nine-month statements; the local cache retains the latest
20. Apple’s latest cached fiscal 2026 Q3 row includes revenue
of $109.417 billion, net income of $29.789 billion, derived standalone-quarter
operating cash flow of $34.369 billion, and a 16.4% year-over-year revenue
increase. The SEC filing was dated July 31, 2026; the replay conservatively
admits it starting August 1 UTC. The [filed 10-Q](https://www.sec.gov/Archives/edgar/data/320193/000032019326000020/aapl-20260627.htm)
is the source for the reported figures. The cash-flow quarter is derived from
SEC year-to-date facts, not quoted as an independently reported quarterly line.

On the same cached 276-bar Apple sample, technical-only replay resolved 17
directional calls and hit 7 (**41.18%**, average captured move **-0.48%**).
Technical plus filing-date-gated SEC fundamentals resolved 16 and hit 7
(**43.75%**, average captured move **-0.47%**). The latter abstained on one more
call, so the percentages are not a matched-trade improvement claim. Both
average captured moves are negative: **no alpha was detected**.

For a fairer side-by-side check, run the offline matched-date experiment:

```bash
.venv/bin/python -m alpha_engine.validation.compare_context AAPL
```

The Apple experiment now reserves every candle dated **September 22, 2026 or
later** as a future holdout. The comparison excludes those bars even after the
price cache grows, and reports how many were reserved. A holdout means data kept
unseen while choosing or changing a model; do not tune against it or use the
general backtest command on that period while developing this Apple model.

For independent historical headlines, the existing Finnhub adapter can export
an unpruned research file. Put your own `FINNHUB_API_KEY` in `.env` (never in a
chat or commit), then run:

```bash
.venv/bin/python -m alpha_engine.ingestion.finnhub_news AAPL --days 365
.venv/bin/python -m alpha_engine.validation.compare_context AAPL \
  --news-file data/research/AAPL_finnhub_news.json
```

The ordinary news cache retains only 30 days, so the archive is separate and
gitignored. The exporter refuses an empty API response and will not overwrite
an existing archive. It records the requested date range, provider, and retrieval
time; the comparison refuses an archive whose request misses the development
period or the first signal's 21-day news lookback. Check its printed first/last headline dates too: a request for
365 days does not prove the provider actually supplied a complete year. There is no
Finnhub key in this environment, so **historical company-news coverage has not
yet been obtained or tested**. A public alternative was tried twice on
September 21, 2026 and returned HTTP 429 both times. Historical headlines are
also a retrospective feed, not an archived copy of exactly what was known on
each day; a true live holdout needs prospective collection.

The comparison uses cached candles and filings, plus the optional news file,
and prints the exact dates scored by each pair. It shows a news arm only when
a headline had a nonzero vote on at least one sampled date, and calls the
comparison available only when a shared directional call resolved. A tagged
but unscorable headline cannot masquerade as a tested sentiment model.
On the current 276-bar sample, each version had **15 matched resolved calls**,
**7 hits (46.67%)**, and a **-0.2831% average captured move**. A two-sided
10-basis-point-per-side cost estimate lowers that to **-0.4831% per call**.
Fundamentals made one fewer independent directional call, but did not improve
the shared calls; even the **11 shared calls on which fundamentals actively
voted** had identical outcomes. Apple-tagged news remains at zero, so there is
no news result.
The +47.58% buy-and-hold return spans the full sample; it is context, not a
directly comparable account return. These are correlated signal outcomes, not
an executable trading strategy or proof of alpha.

## Account-level signal experiment

The matched-date check above asks whether individual calls were right. The
trade replay asks what an account would have done under one fixed rule: bullish
means 100% long, bearish means 100% short, neutral means cash. Each decision is
made at one daily candle's close, fills at the **next open**, and holds until
the following open. The last position is closed at the last candle's close.
Position changes and final liquidation pay 10 basis points (0.10%) per side.
It is an experiment in `validation/`, not a new live trading strategy.

Run it offline from the existing cache:

```bash
.venv/bin/python -m alpha_engine.validation.trade_experiment AAPL MSFT GOOGL NVDA
# Once a dated company-news archive exists:
.venv/bin/python -m alpha_engine.validation.trade_experiment AAPL \
  --news-file data/research/AAPL_finnhub_news.json
```

The replay cuts every stock off before the fixed Apple holdout date so the
cross-stock development check cannot peek into Apple's reserved period. The
80-bar warmup and next-open fill leave about 194–195 daily return intervals
per stock. The buy-and-hold benchmark uses **that same interval**, with the same
entry/exit fees; it is not the +47.58% full-sample figure above. The candidate
does not generate a news arm unless news actually votes. On the cached
development sample ending August 31, 2026:

| Stock | Technical | Technical + fundamentals | Same-window buy-and-hold |
|---|---:|---:|---:|
| AAPL | +0.21% | +0.60% | +18.73% |
| MSFT | -2.97% | -2.97% | +3.95% |
| GOOGL | -4.69% | -4.69% | +17.68% |
| NVDA | -47.99% | -47.99% | +18.19% |

Apple fundamentals voted on 145 closes, but the extra context barely changed
the account outcome. No fundamentals voted on the other three cached stocks;
their identical columns are **missing evidence**, not proof that fundamentals
never matter. There were zero Apple news votes. The report also shows a
volatility-matched buy-and-hold comparison, but its weight is selected from the
*full sample's realized volatility* (hindsight), so it is a diagnostic rather
than a strategy one could have executed. It still comfortably beat the Apple
candidate. The replay omits slippage, short-borrow costs, dividends, taxes and
market impact. These results reject a present claim of demonstrated alpha; they
do not justify tuning the rule on this same sample until it looks profitable.

At 0 / 10 / 25 basis points per side, Apple's technical-plus-fundamental
account returned **+6.82% / +0.60% / -8.08%**; same-window buy-and-hold returned
**+18.97% / +18.73% / +18.37%**. The frequent position changes make the
candidate unusually sensitive to costs. Even zero modeled costs do not close
the gap, and omitted slippage and short-borrow costs would not improve it.

At the latest cached price, technical evidence was conflicted and neutral.
Fundamentals were bullish with a 0.2333 source weight, but the final decision
remained neutral, exactly as the technical-first rule requires.

## Point-in-time repair made for a valid replay

The engine backtest now replays cached news and fundamentals in addition to
price and macro data. This needed one important guard:

- A report's quarter-end date is not the day investors learned its contents.
- `Fundamentals.available_at` stores FMP's publication timestamp, or the day
  after the SEC filing date when only a date is available.
- During a replay, a filing becomes visible only at that timestamp.
- Old records without a publication timestamp abstain rather than pretending
  they were public at quarter end.

News is likewise filtered to headlines published by the simulated candle time,
and its recency calculation uses that historical time rather than today's date.
Focused tests add extreme future news and fundamentals and verify that an earlier
signal is unchanged.

This is a filing-date-gated replay, not a perfect historical data-vintage
archive: the SEC company-facts endpoint is a current snapshot, and later SEC
corrections or removals may change which old facts are present. The parser
selects the earliest filed version it can see, but a truly untouched historical
study would archive each feed snapshot as it arrived.

The existing public RSS refresh fetched 1,290 headlines, but **zero were tagged
AAPL**, so the Apple news-sentiment source correctly abstained with zero weight.
The official [Apple Newsroom RSS feed](https://www.apple.com/newsroom/rss-feed.rss)
was also checked: its latest 20 product/entertainment headlines had no matches
for this finance lexicon. Treating company promotional copy as independent
market sentiment would be misleading. A Finnhub key or another independently
verifiable company-news source is needed for a real news comparison.

## Five strategy candidates

Five small, auditable ideas are available through the existing strategy engine:

| Candidate | Idea |
|---|---|
| SMA crossover | Follow medium-term trend changes. |
| RSI reversal | Fade an overextended price move. |
| Supertrend flip | Follow an ATR-based trend-state change. |
| Donchian breakout | Follow a close beyond the previous range. |
| Momentum + volume | Follow a strong move only with volume participation. |

The last three were added as minimal built-ins, inspired by common open-source
algorithmic-trading workflows but implemented locally from the mathematical
ideas. No OpenAlgo source code was copied.

## Measured AAPL result

The cached daily sample contained 276 bars from 2025-07-28 through 2026-08-31.
Apple buy-and-hold returned **+47.58%** over that full sample (before fees).
The strategy runs below charge 10 basis points per side on position changes.
They were remeasured after fixing the strategy engine's next-open fill: its
earlier close-to-close calculation mistakenly credited positions with an
overnight gap before the trade could fill. These replace the old figures:

| Candidate | Return | Sharpe | Maximum drawdown | Trades |
|---|---:|---:|---:|---:|
| SMA crossover | +3.36% | 0.248 | -15.31% | 10 |
| RSI reversal | +7.72% | 0.414 | -19.89% | 3 |
| Supertrend flip | -9.92% | -0.350 | -18.03% | 7 |
| Donchian breakout | -21.34% | -0.802 | -37.23% | 7 |
| Momentum + volume | -30.16% | -1.264 | -42.78% | 8 |

All five passed the automated lookahead check. Passing that check means the
replay is not obviously cheating; it does not make a poor result good.

## Stress tests

Transaction-cost sensitivity, shown at 0 / 2 / 10 / 25 basis points:

| Candidate | Returns across costs |
|---|---|
| SMA crossover | +5.35% / +4.95% / +3.36% / +0.45% |
| RSI reversal | +8.26% / +8.16% / +7.72% / +6.92% |
| Supertrend flip | -8.74% / -8.98% / -9.92% / -11.66% |
| Donchian breakout | -20.31% / -20.51% / -21.34% / -22.86% |
| Momentum + volume | -29.11% / -29.32% / -30.16% / -31.72% |

Nearby parameters were unstable. For example, SMA 5/20 returned -15.84%, 9/21
returned +3.36%, and 20/50 returned -12.72%. Donchian lookbacks 10, 20, and 40
returned -1.27%, -21.34%, and +4.22%; the positive case had only three trades.
This is a warning for parameter-selection luck, not a reason to select the best
row after seeing it.

The first/second sample halves also changed behavior sharply. SMA moved from
-4.02% to +4.96%; RSI from +2.15% to -5.41%; Momentum + Volume from +5.43% to
-30.50%. These halves are diagnostics, not untouched holdouts, but the instability
is exactly what a robust alpha should not show.

## Cross-sectional factor experiment

`quant/cross_sectional.py` adds a separate research-only evaluator for at least
four cached equities. It standardizes six price/volume factors across stocks,
fits ridge regression only on forward returns whose entire horizon is already in
the past, and compares its long/short result with deterministically shuffled
scores. It intentionally does not feed the live signal path.

On the locally cached AAPL, GOOGL, MSFT, and NVDA data it produced only seven
independent 10-day evaluation periods: **-22.85%**, Sharpe **-1.69**, mean rank
IC **-0.0857**, versus **-1.94%** for the shuffled baseline. The sample is tiny
and the candidate failed even that weak baseline, so the result is “no alpha
detected.”

Run it with:

```bash
.venv/bin/python -m alpha_engine.quant.cross_sectional AAPL MSFT GOOGL NVDA
```

## Visual verification

Generate a TradingView Lightweight Charts report containing candles, confirmed
entry markers, and the account equity curve:

```bash
./start.sh strategy-backtest AAPL --strategy SMACrossover \
  --days 365 --no-refresh --cost-bps 10 --chart aapl-sma.html
```

The report includes the required TradingView attribution and a research-only
warning. Its embedded data and HTML structure are tested. On September 22, 2026,
the optional Chromium browser suite passed all 22 tests, and the generated Apple
chart was rendered and visually checked without JavaScript errors. Entry arrows
now mark the next-open fill bar, not the preceding signal candle.

## Next steps and release gates (set September 22, 2026)

These are research gates, not a promise that the model will become profitable.
An *out-of-sample holdout* is future data left unseen while deciding how the
model works. Opening it to pick better settings would make it in-sample again.

1. **Complete the development-data audit.** Done for the local sample: 276
   daily candles through August 31, 2026; 20 SEC fundamental periods; zero
   Apple-tagged headlines. The matched experiment uses 10-bar steps, a 10-bar
   swing outcome, and a 10-basis-point-per-side cost *proxy*. Keep those settings
   fixed for this candidate. The proxy is not an account return.
2. **Obtain independent company news.** Pending an owner-supplied Finnhub key in
   `.env`, or an equivalently sourced archive. Export it with the command above,
   verify the requested and returned date spans, then run the comparison with
   `--news-file`. The report must show nonzero `news_active_dates` before making
   any claim about sentiment. Never copy a key or licensed headline archive into
   Git. Historical API output still has revision/vintage uncertainty.
3. **Freeze and collect the future holdout.** The Apple comparison structurally
   excludes every bar on or after September 22, 2026. The existing daily job
   already records AAPL signals. Do not change this candidate's rules based on
   holdout prices or use the general backtest to tune on them. Earliest planned
   review: March 22, 2027, and only if at least 10 **non-overlapping resolved**
   10-bar calls exist. If not, keep collecting; do not lower the gate afterward.
4. **At that one review, compare fairly.** The fixed next-open, costed
   trade-level replay is implemented for development data and performed poorly.
   Use the recorded calls and price
   outcomes, report abstentions and data gaps, and compare the same dates for
   technical-only, technical-plus-fundamental, and news-confirmed versions.
   If a version has no active news vote or too few calls, report "insufficient
   evidence," not a zero or an invented hit rate. The risk-matched benchmark is
   ex-post only; a prospective comparison must set its risk rule in advance.
5. **Expand only after a positive, stable holdout.** Check nearby parameter
   settings and several stocks/market regimes, without choosing the best row
   after seeing those results. If the holdout fails, keep the result and label
   the candidate rejected or unproven; do not rename it alpha.

## Conclusion and the missing evidence

The project now has the correct mechanics for combined technical, fundamental,
and news research, two safe narrative modes, point-in-time context replay, five
strategy candidates, stress tests, and an interactive chart artifact. It does
**not** have demonstrated Apple alpha.

The immediate missing inputs are independent company-news history and elapsed
future time. Until the gates above are met, every model here is a research
candidate rather than demonstrated alpha.
