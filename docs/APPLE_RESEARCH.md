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
Apple buy-and-hold returned **+47.58%** over that same sample. With a 10 basis
point transaction cost on each position change:

| Candidate | Return | Sharpe | Maximum drawdown | Trades |
|---|---:|---:|---:|---:|
| SMA crossover | +7.26% | 0.400 | -15.56% | 10 |
| RSI reversal | +5.76% | 0.339 | -19.81% | 3 |
| Supertrend flip | -12.96% | -0.491 | -20.38% | 7 |
| Donchian breakout | -22.53% | -0.860 | -38.17% | 7 |
| Momentum + volume | -28.14% | -1.151 | -41.23% | 8 |

All five passed the automated lookahead check. Passing that check means the
replay is not obviously cheating; it does not make a poor result good.

## Stress tests

Transaction-cost sensitivity, shown at 0 / 2 / 10 / 25 basis points:

| Candidate | Returns across costs |
|---|---|
| SMA crossover | +9.32% / +8.91% / +7.26% / +4.23% |
| RSI reversal | +6.29% / +6.18% / +5.76% / +4.96% |
| Supertrend flip | -11.78% / -12.02% / -12.96% / -14.72% |
| Donchian breakout | -21.49% / -21.70% / -22.53% / -24.07% |
| Momentum + volume | -27.03% / -27.25% / -28.14% / -29.77% |

Nearby parameters were unstable. For example, SMA 5/20 returned -17.77%, 9/21
returned +7.26%, and 20/50 returned -10.47%. Donchian lookbacks 10, 20, and 40
returned -9.24%, -22.53%, and +3.03%; the positive case had only three trades.
This is a warning for parameter-selection luck, not a reason to select the best
row after seeing it.

The first/second sample halves also changed behavior sharply. SMA moved from
-0.75% to +4.95%; RSI from +0.82% to -5.56%; Momentum + Volume from +6.83% to
-31.99%. These halves are diagnostics, not untouched holdouts, but the instability
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
warning. Its embedded data and HTML structure are tested. Automated visual
rendering was not available in the restricted browser used for this work, so a
human should open the generated file and confirm its final appearance before it
is published.

## Conclusion and the missing evidence

The project now has the correct mechanics for combined technical, fundamental,
and news research, two safe narrative modes, point-in-time context replay, five
strategy candidates, stress tests, and an interactive chart artifact. It does
**not** have demonstrated Apple alpha.

The next useful experiment is not another indicator. It is more honest data:

1. configure Finnhub or another independent company-news source and build
   point-in-time headline history;
2. reserve a genuinely untouched future period before choosing parameters;
3. compare against buy-and-hold and a risk-matched benchmark after costs;
4. require stable results across nearby parameters and multiple market regimes;
5. collect substantially more than three to ten trades before drawing a conclusion.

Until those conditions are met, every model here should be called a research
candidate rather than alpha.
