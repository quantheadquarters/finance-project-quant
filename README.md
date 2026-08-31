# Alpha Engine

An open, deterministic research engine for market signals across crypto, US
equities, Indian equities, Indian F&O, and forex.

It reads price history and market data, runs 20+ independent analyzers over it,
and produces a signal with a direction, a calibrated confidence, a full audit
trail of every contributing opinion, and the price at which its own reasoning
would be wrong.

> ## ⚠️ Research only
>
> **This is not financial advice.** It is not a recommendation and not a
> solicitation to trade. The analyzers are honest scaffolding, not proven
> money-makers — measured against real outcomes they perform at roughly a coin
> flip, and this project says so rather than hiding it. Anyone acting on this
> output does so entirely at their own risk.

> ### The measured result
>
> The engine has been scored against **6,788 signals across 7 assets and up to
> 5 years** of history: **+0.0% directional edge** over the base rate. It does
> not predict. [FINDINGS.md](FINDINGS.md) has the method, the numbers, and the
> four plausible-looking results that turned out to be measurement errors.
>
> That is the honest state, and finding it out is what this project is for.

## What this measures, and what it doesn't

This is a **measuring instrument, not a forecaster**. The distinction decides
whether it is useful to you, so it is worth being blunt about both halves.

### What it measures reliably

| It answers | How, and why you can trust the answer |
|---|---|
| **Were the engine's own calls right?** | `backtest` replays history through `signal_at()`, which truncates the series *before* any analyzer runs. No-lookahead is structural — a caller cannot leak the future in even by accident. |
| **What would my own trading rule have done?** | `strategy-backtest` gives trades, equity curve, Sharpe, and drawdown. It also re-runs your rule on truncated history and reports any bar whose signal changed, so lookahead is *detected* rather than assumed away. |
| **Does this factor predict anything, or am I fooling myself?** | `factors` scores 495 factors by rank IC and compares each against the \|IC\| that the best of 495 *random* factors would reach on that factor's own sample size. On BTC, 385 of 495 fail that line. |
| **Is a data source dead or just quiet?** | Every adapter records an item count per feed, so `health` distinguishes "no news today" from "this scraper broke in March". Scrapers fail silently; this is the countermeasure. |
| **How confident should a confidence score make me?** | `record-stats` and `calibrate` score logged signals against what actually happened, bucketed by stated confidence. |

The common thread: every one of these can return a **negative** answer, and the
code is built so the negative answer is the easy one to get. That is the whole
design.

### What it does not measure, and cannot

- **It does not predict prices.** Measured over 6,788 signals: +0.0% edge over
  the direction-matched base rate. Not "modest edge" — no detectable edge.
  See [FINDINGS.md](FINDINGS.md).
- **A high-ranked factor is not alpha.** Rank IC is in-sample association. The
  noise floor filters the obviously-random ones; it cannot make survivors real.
  Only out-of-sample confirmation does that, and this repo does not do it for you.
- **A backtest is not a forecast.** No slippage model survives contact with a
  thin book, and past-fitted parameters are the oldest trap in the field.
- **Confidence is not probability.** It is a weighted vote of analyzers whose
  individual reliability is, so far, measured at approximately coin-flip.
- **It has no view on position sizing or your risk tolerance.** `risk` reports
  concentration and VaR on a portfolio you describe; it does not know your account.

### Who it is actually for

Someone who wants to **test a market idea and find out it doesn't work, cheaply**
— before risking money on it. The instrument is the product. The 504-factor
registry, the two backtesters, and the no-lookahead guarantees exist so that a
null result is trustworthy, and [FINDINGS.md](FINDINGS.md) exists because null
results are worth publishing.

If you are looking for signals to trade, this will disappoint you, and it is
designed to disappoint you honestly rather than late.

## Where to look

Thirteen documents is a lot. Read down this list only as far as you need.

| If you want to… | Read |
|---|---|
| **Understand what this is** | [HOW_IT_WORKS.md](HOW_IT_WORKS.md) — plain English first, then technical |
| **Install and run it** | [GETTING_STARTED.md](GETTING_STARTED.md) |
| **Know whether it works** | **[FINDINGS.md](FINDINGS.md)** — measured results, including the null ones |
| **Know what's broken** | [BUGS.md](BUGS.md) — open defects and fixed ones, with their evidence |
| **Run it on a schedule** | [RUNNING_IT.md](RUNNING_IT.md) — the daily job and source health |
| **Change the code** | [AGENTS.md](AGENTS.md) — architecture, extension points, and the gotchas that cost someone a day |
| **Know why it is built this way** | [context.md](context.md) — the non-negotiable design rules |
| **See what is planned** | [FUTURE_WORK.md](FUTURE_WORK.md) — the roadmap, rewritten around the measurement |
| **Judge it for production** | [AUDIT.md](AUDIT.md) — a full security/reliability/performance review |
| **Write an analyzer** | [docs/analyzer-guide.md](docs/analyzer-guide.md) |
| **Write a strategy** | [strategies/README.md](strategies/README.md) |
| **Deploy it** | [docs/deployment.md](docs/deployment.md) |
| **See what changed** | [CHANGELOG.md](CHANGELOG.md) |

`CLAUDE.md` and `AGENTS.md` are instructions for AI coding assistants working in
this repo. They are accurate and worth reading as a human too — `AGENTS.md` in
particular records every trap this codebase has actually sprung.

---

## Get it running in one command

You do not need to know Python. Copy these three lines into a terminal:

```bash
git clone https://github.com/infoshubhjain/finance-project-quant.git
cd finance-project-quant
./start.sh
```

That is the whole setup. It will:

1. check you have Python 3.10 or newer (and tell you exactly how to install it
   if you don't),
2. build an isolated environment inside the project folder — nothing else on
   your computer is touched,
3. install what it needs,
4. generate a few real signals so there is something to look at,
5. open it in your browser at **http://localhost:8000**.

You land on two doors: **Dashboard** (everything the engine has decided — no
keys, no AI) and **Terminal** (ask questions in plain English; you supply your
own LLM key). The same tools are on an HTTP API and over MCP.

Press `Ctrl+C` in the terminal to stop it.

**No API key is needed.** Crypto and US equities work out of the box. An LLM key
is only for the Terminal, and it is yours — the server never stores one.

`start.sh` is also the only command you need afterwards — `./start.sh scan BTC`,
`./start.sh doctor`, and so on. It runs from any working directory (call it by
its full path) and always keeps its environment and data inside the project
folder.

<details>
<summary><b>Windows users — read this first</b></summary>

`start.sh` is a shell script, so it needs a Unix-style shell. Two easy options:

**Git Bash** (simplest) — install [Git for Windows](https://git-scm.com/download/win),
then right-click in the project folder and choose "Git Bash Here". Run the
commands above there.

**WSL** — run `wsl --install` in PowerShell as Administrator, restart, then use
the Ubuntu terminal.

Either way, make sure you ticked **"Add Python to PATH"** when installing
Python. It is the single most common cause of setup failure.

If you would rather not use a shell script at all, everything works through
Python directly:

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
python -m alpha_engine.cli.main scan BTC
python -m alpha_engine.cli.main dashboard
```

</details>

<details>
<summary><b>Something went wrong</b></summary>

Run the built-in diagnostic:

```bash
./start.sh doctor
```

It prints your Python version, whether the engine installed, how many signals
you have, which optional keys are set, and then tries a real scan to confirm the
whole path works.

| Symptom | Cause | Fix |
|---|---|---|
| `python3: command not found` | Python missing or not on PATH | Install from python.org; tick "Add to PATH" |
| `Could not create the virtual environment` | `venv` module missing | `sudo apt install python3-venv` |
| `Installation failed` | Usually no internet | Check connection, then `pip install -e ".[dev]"` to see the real error |
| Dashboard is empty | No signals recorded yet | `./start.sh scan BTC` |
| `HTTP 429` while scanning | Free API rate limit | Wait a few minutes. The cache exists to absorb this — don't retry in a loop |
| `Permission denied: ./start.sh` | Script not executable | `chmod +x start.sh` |
| `CERTIFICATE_VERIFY_FAILED` on **every** source | Python has no CA certificates | See below — `./start.sh doctor` names the fix |

</details>

<details>
<summary><b><code>CERTIFICATE_VERIFY_FAILED</code> — every source fails at once</b></summary>

If every asset fails in milliseconds with `[SSL: CERTIFICATE_VERIFY_FAILED]`,
including the fallback sources, Python cannot verify **any** website's identity.
This is an install problem, not a network one — and not an engine one.

Confusingly it produces two different messages from the same cause, depending on
whether a server sends the top of its certificate chain:

```text
CoinGecko / Yahoo : unable to get local issuer certificate
Binance           : self-signed certificate in certificate chain
```

Confirm it:

```bash
python3 -c "import ssl; print(len(ssl.create_default_context().get_ca_certs()))"
```

`0` means an empty trust store (a healthy machine prints a few hundred). Fix it
with whichever matches how Python was installed:

```bash
# Installed from python.org (the usual cause on macOS):
open "/Applications/Python 3.13/Install Certificates.command"   # match your version

# Homebrew / pyenv / conda:
pip install certifi
export SSL_CERT_FILE="$(python -c 'import certifi; print(certifi.where())')"
```

`./start.sh doctor` reports the trust store and prints these instructions.

> **Do not disable certificate verification to work around this.** It turns off
> the check that data came from the source it claims to — and this engine makes
> decisions from that data.

</details>

---

## Don't want to memorize commands?

```bash
./start.sh menu
```

A numbered list — open the dashboard, analyze an asset, run a backtest, see how
past signals turned out. Pick a number, press enter.

---

## What you can actually do with it

### Look at one asset

```bash
./start.sh scan BTC          # crypto
./start.sh scan AAPL         # US stock
./start.sh scan RELIANCE.NS  # Indian stock
./start.sh scan NIFTY        # Indian F&O (needs a broker key)
./start.sh scan EURUSD       # forex (needs OANDA credentials)
```

You get JSON with the direction, confidence, invalidation level, a plain-English
thesis, and every analyzer's individual vote and weight. The market is
auto-detected from the symbol; override with `--market`.

### Get the full quantitative picture

```bash
./start.sh report BTC
```

Trend strength, momentum, volatility regime, volume structure, and reads from
three statistical models (Kalman filter, GARCH, hidden Markov model) — all
implemented in dependency-free Python.

### Find out which factors actually predict anything

```bash
./start.sh factors BTC                    # top 40 of 504 factors
./start.sh factors BTC --clusters         # which factors are the same idea
./start.sh factors BTC --family momentum  # one family only
./start.sh factors BTC --top 0 --json     # everything, machine-readable
```

Each factor is scored by rank IC — how well its value at a point in time
correlated with what happened next.

**Read the noise floor line at the bottom of the output.** It tells you what the
luckiest of 500 completely random factors would have scored on your amount of
data. If the top factor doesn't clear that line, the ranking is consistent with
every factor being noise, and the engine says so in those words. Testing 500
things and reporting the winner without that correction is how backtests lie.

### Check whether any of it works

```bash
./start.sh backtest BTC --days 365   # replay history, no lookahead
./start.sh record-stats              # score real recorded signals
./start.sh calibrate                 # re-derive analyzer reliability
```

`backtest` replays history through the exact same pipeline with a hard
no-lookahead guarantee. `record-stats` scores the signals you actually generated
against what the price actually did.

### Run it on a schedule

```bash
./start.sh scan-all                    # everything in portfolio.json
./start.sh batch --output report.json  # cron-friendly
./start.sh ingest                      # refresh news / on-chain / fundamentals
./start.sh orchestrate --news          # headlines trigger targeted re-scans
```

`ingest` also scrapes the **FOMC meeting calendar** from federalreserve.gov, so
the engine automatically lowers confidence in the days before a rate decision
without you entering anything. Events it cannot scrape — RBI MPC dates, CPI
releases, earnings — go in `calendar.json` (copy `calendar.example.json`).

`orchestrate --news` is the event-driven path: recent headlines about assets you
follow become high-priority re-scans of *just those assets*, ahead of the
routine sweep.

To run it every day, one command sets up the scheduled job:

```bash
./scripts/install-cron.sh          # 9am daily; --at 18:30 for another time
```

That installs `scripts/daily.sh`, which refreshes context data, scans the
portfolio, and then checks whether any data source has gone quiet. It handles
locking (never two runs at once), stale-lock recovery, a hard timeout, and log
rotation. See [RUNNING_IT.md](RUNNING_IT.md).

### Knowing when a data source dies

```bash
./start.sh health     # per-source status
./start.sh doctor     # that, plus setup, cron state and last run
```

This matters more than it sounds. Every adapter here degrades to empty rather
than crashing, so a scraper that broke in March keeps exiting successfully and
returning nothing — and every signal afterwards is quietly weaker. `health`
tracks when each source last produced data and flags the ones that have gone
silent, separating *broken* from *deliberately switched off*:

```text
source                   status detail
------------------------------------------------------------------------
news.fed_press           ok     last data 0.1d ago, 240 items total
news.nse_announcements   WARN   no data for 12.0d (expected within 3d)
news.rbi_press           FAIL   4 consecutive errors: HTTP 503
news.sec_edgar           off    SEC_USER_AGENT is not set
```

### Backtest your own strategy

`backtest` asks whether the *engine's* signals were right. `strategy-backtest`
asks a different question: if you had actually traded your own rule, what would
the account have done?

```bash
./start.sh strategies                       # what's available
./start.sh strategy-backtest BTC --strategy SMACrossover --trades 5
./start.sh strategy-backtest NIFTY --strategy RSIReversal \
    --param length=21 --option NIFTY24000CE --trade-on option
```

A strategy is a small Python class in `strategies/`. Implement one method and it
is discovered automatically:

```python
from alpha_engine.strategy.base import BaseStrategy
from alpha_engine.strategy.indicators import ema, crossed_above, crossed_below

class MyStrategy(BaseStrategy):
    name = "My Strategy"
    params = {"fast": 9, "slow": 21}

    def generate_signals(self, candles):
        fast = ema(candles, self.params["fast"])
        slow = ema(candles, self.params["slow"])
        out = [0] * len(candles)
        for i in range(len(candles)):
            if crossed_above(fast, slow, i):
                out[i] = 1
            elif crossed_below(fast, slow, i):
                out[i] = -1
        return out
```

You get trades, an equity curve, Sharpe / Sortino / Calmar, max drawdown, win
rate and profit factor. Two guards are built in rather than left to discipline:
the position is filled one bar *after* the signal, and every run re-executes your
strategy on truncated history to catch it reading future bars. **If it reports a
lookahead violation, every other number in that report is void** — which is the
point of checking.

The `--option` flag is the idea worth stealing: a signal on the index does not
become a trade until the option's own chart confirms it. Override
`verify_on_option` to confirm on volume, open interest, or anything else in your
option data.

### Use it from an AI assistant

The engine speaks [MCP](https://modelcontextprotocol.io), so Claude Code, Cursor
or any MCP client can call it directly:

```json
{
  "mcpServers": {
    "alpha-engine": {
      "command": "/absolute/path/to/finance-project-quant/start.sh",
      "args": ["mcp"]
    }
  }
}
```

Nine read-only tools: `scan`, `report`, `backtest`, `options_backtest`,
`factors`, `record_stats`, `health`, `list_strategies`, `strategy_backtest`.

This fits the architecture unusually well, and not by luck. MCP means the model
*calls* deterministic tools and *reads* results — it never computes the numbers.
That is exactly this project's core rule, so an assistant structurally cannot
invent a figure; it can only ask the engine questions and relay what tested
Python answered. The research-only disclaimer is attached to every payload, so
it travels wherever the output gets pasted.

### Use it over HTTP

The same tools, no MCP client required. `./start.sh dashboard` serves them:

```bash
curl localhost:8000/api/v1/tools                    # self-describing catalogue
curl "localhost:8000/api/v1/tools/scan?asset=BTC"
curl -X POST localhost:8000/api/v1/tools/strategy_backtest \
     -H 'content-type: application/json' \
     -d '{"asset":"BTC","strategy":"SMACrossover","params":{"fast_length":5}}'

# MCP over HTTP, for remote MCP clients
curl -X POST localhost:8000/api/v1/mcp \
     -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

All three transports dispatch into one table (`alpha_engine/toolkit.py`), so
they cannot drift apart.

It binds `127.0.0.1` and is read-only by default. Exposing it needs deliberate
choices: `ALPHA_API_KEY` is **required** to bind any other interface (the server
refuses rather than warns), `--allow-writes` is needed before any caller can
append to the signal log, and requests are rate-limited per IP. No route accepts
strategy source code — loading a strategy executes Python, and that is remote
code execution over a network, not a feature.

### Talk to it

`http://localhost:8000/terminal` is a chat where an AI answers by calling the
engine's tools. You bring your own key — OpenAI, Anthropic, OpenRouter or Groq
— and it is used for the request and dropped. The server stores no keys; there
is nothing to leak.

```bash
./start.sh terminal "does BTC look bullish, and how much should I trust that?"
export LLM_API_KEY=sk-...   # or --api-key
```

The model is instructed never to compute or recall a number. But an instruction
is not a guarantee, so the real one is structural: **every tool call and its raw
result is shown next to the answer.** Any figure in the prose can be checked
against the payload that produced it, without trusting the model at all. The
terminal also cannot write — the write-capable arguments are stripped from the
schema it is given, so it never even sees them.

---

**Measured, not claimed:** [FINDINGS.md](FINDINGS.md) holds what the engine has
actually been shown to do — currently +0.0% directional edge over 6,788 signals.
Read it before trusting anything here.

## The rule everything is built around

> **Decision-bearing numbers come only from deterministic, tested Python. The
> LLM is optional, key-gated, confined to `narrative/`, and may write only the
> `thesis` prose — never a number.**

If a language model set the confidence score, you could never replay history and
check whether the engine was right, because the model might answer differently
tomorrow. Determinism is what makes a track record verifiable, and a track
record you cannot verify is marketing.

Consequences you can check in the code: no network calls or randomness in
`analyzers/` or `synthesis/`; the default path never needs an API key; the
signal log is append-only; the disclaimer is never weakened.

---

## Capability matrix

| Market | Data source | Key needed? | Analyzers |
|---|---|---|---|
| Crypto | CoinGecko → Binance fallback | No | trend, momentum, volume, on-chain positioning |
| US equity | Yahoo Finance | No | trend, momentum, volume, macro, fundamentals\* |
| Indian equity | Yahoo Finance, NSE disclosures | No | Indian trend, RBI-aware macro, FII/DII flows |
| Indian F&O | Breeze / Angel One | Yes | PCR, max pain, OI walls, OI shift |
| Forex | OANDA | Yes | trend, carry, dollar cycle, INR band |

<sub>\* fundamentals need a free FMP key</sub>

### Data sources (17 adapters)

**Keyless:** CoinGecko, Binance spot, Binance futures (funding + open interest),
Yahoo Finance, World Bank, SEC EDGAR, Fed press, RBI press, NSE announcements,
NSE FII/DII flows, RBI policy rates, FOMC meeting calendar.

**Key-gated** (all free tiers, all degrade gracefully): FRED, Finnhub news, FMP
fundamentals, Glassnode on-chain, CoinGecko Pro, OANDA, Breeze, Angel One, Dhan.

A missing key never breaks a scan — the affected analyzer abstains with weight
zero and says so in its detail string.

**On the scraped sources** (NSE, RBI): scraping is a contract nobody signed, and
those sites can change a field name any Tuesday. Those adapters validate the
shape of what they receive and print a loud `CONTRACT BROKEN` warning while
returning nothing, rather than a plausible-looking wrong number. Empty output
from a scraper always means "could not read it", never "there was nothing there".

### Factor library — 504 factors

| Family | Count | Examples |
|---|---|---|
| `ma_structure` | 111 | SMA/EMA/WMA distance, slope, crossover spreads |
| `distribution` | 78 | skew, kurtosis, autocorrelation, variance ratios, Hurst |
| `volatility` | 73 | realized, Parkinson, Garman-Klass, Yang-Zhang, EWMA |
| `trend_quality` | 52 | regression slope/R²/t-stat, ADX, efficiency ratio |
| `momentum` | 49 | raw, vol-normalized, skip-a-bar, acceleration, rank |
| `range_structure` | 48 | distance to highs/lows, Donchian, Bollinger, gaps |
| `volume` | 45 | OBV slope, Amihud illiquidity, VWAP distance, CMF |
| `risk_adjusted` | 17 | Sharpe, Sortino, Calmar, Ulcer index, drawdown |
| `oscillator` | 16 | RSI, stochastics, CCI, Williams %R, money flow |
| `model` | 15 | Kalman distance, GARCH forecast, HMM regime |

Every factor is pinned against lookahead by a test that runs over the whole
registry: its value at bar `t` must be identical whether computed on the full
series or on a series truncated at `t`. Adding a factor is one dictionary entry
and nothing else — it then appears in `factors` output, gets IC-scored, and is
covered by that test automatically.

Nine of the 504 (GARCH, HMM) are tagged `cost="slow"` — they refit a model at
every bar and are ~100× the rest. They are excluded from the default panel;
`--all-factors` opts in, at the price of turning a ~4-second command into
minutes.

**How much history you have decides how many factors exist.** Each one declares
`min_bars` and returns `None` — never a plausible-looking wrong number — until it
has enough data. The `factors` command already defaults to **365 days** for this
reason, so out of the box it produces 494 of the 495 fast factors:

| History | Fast factors that can produce a value |
|---|---|
| 90 bars | 308 of 495 |
| 252 bars (~1 trading year) | 457 of 495 |
| 365 bars (the `factors` default) | 494 of 495 |

Coverage only shrinks if you *shorten* the window with `factors BTC --days 90`.
The factor library is used only by `factors` and `backtest` — a plain `scan`
never touches it, so a scan's shorter 90-day window has no effect on factors. If
a factor you expected is missing from `factors` output, the usual reason is that
the asset itself has fewer than 365 bars of real history.

---

## Optional API keys

Everything above works without any of these. Copy `.env.example` to `.env` and
fill in only what you want.

| Variable | Unlocks | Free key |
|---|---|---|
| `FRED_API_KEY` | US macro (rates, CPI, unemployment) | https://fred.stlouisfed.org |
| `FINNHUB_API_KEY` | Company-tagged news | https://finnhub.io |
| `FMP_API_KEY` | Company fundamentals | https://financialmodelingprep.com |
| `GLASSNODE_API_KEY` | Crypto on-chain flows | https://glassnode.com |
| `SEC_USER_AGENT` | SEC EDGAR filings | **no signup** — just your name + email |
| `LLM_API_KEY` | AI-written thesis prose (never a number) | any provider |
| `OANDA_API_KEY` | Forex price data | https://oanda.com |
| `BREEZE_API_KEY` / `ANGEL_ONE_API_KEY` | Indian F&O chains | broker account |
| `DHAN_ACCESS_TOKEN` | Order placement (paper by default) | broker account |

---

## Project layout

```text
src/alpha_engine/
  schema/signal.py      the contract every layer compiles against
  ingestion/            network adapters (the only layer that touches the net)
  cache/                normalized models + local store with TTLs
  analyzers/            pure functions: data in, SignalSource out
  synthesis/            weighted vote + confidence calibration
  narrative/            the LLM lives here, and only here
  quant/                features, 504-factor registry, ranking, models, reports
  strategy/             your own trading rules + the trade-level backtester
  validation/           append-only log, outcome scoring, backtests, calibration
  orchestrator/         batch runner + event-driven trigger engine
  execution/            order placement (paper-first, live is gated)
  toolkit.py            THE tool table — MCP, HTTP and the AI terminal share it
  cli/main.py           every command
  health.py             source health tracking (makes silent decay visible)
strategies/             drop a BaseStrategy subclass here; it's found automatically
  web/                  dashboard + AI terminal + HTTP API (no build step)
  mcp.py                MCP over stdio (stdlib only, transport only)
mcp_server.py           3-line shim; MCP client configs point at a file path
scripts/daily.sh        the scheduled job: lock, timeout, rotation, health check
scripts/install-cron.sh one-command cron setup
tests/                  ~2520 tests, all network-free
```

---

## Development

```bash
source .venv/bin/activate
pip install -e ".[dev]"

pytest -q                                # ~2520 tests, ~40s, no network
ruff check . && ruff format --check .    # CI gates both
python -m alpha_engine.cli.main scan BTC # manual end-to-end
```

A change is done when all three pass. CI runs on Python 3.11–3.13.

Architecture rules and extension patterns: [AGENTS.md](AGENTS.md) and
[context.md](context.md). Contributing guidelines:
[CONTRIBUTING.md](CONTRIBUTING.md). Roadmap: [FUTURE_WORK.md](FUTURE_WORK.md).
Step-by-step setup with screenshots-level detail:
[GETTING_STARTED.md](GETTING_STARTED.md).

---

## Status: what's built and what isn't

**Built and tested:** the full pipeline, 20+ analyzers, 504 ranked factors, 17
data adapters, no-lookahead backtesting, outcome scoring, reliability
calibration, portfolio risk reporting, the event-driven orchestrator, the web
dashboard, the MCP server, and paper-first order execution.

**Deliberately not built:**

- **The ML layer.** Gated on 12+ months of recorded live outcomes and a
  genuinely untouched holdout period. Building it before then produces prettier
  charts and a worse track record — that is the standard outcome, not a
  hypothetical.
- **DCF and comparables valuation.** Both need assumptions you invent. A number
  you picked does not stop being invented because it is in a spreadsheet.
- **QuantHQ**, the multi-user platform. A different codebase: users, sessions, a
  database, auth. See [FUTURE_WORK.md](FUTURE_WORK.md).

**The honest performance note:** the analyzers are scaffolding with no
demonstrated edge. On BTC backtests they land near a coin flip. The measurement
machinery — backtests, outcome scoring, IC ranking, the noise floor — is the
actual deliverable, and it works. What it currently measures is that this does
not beat the market. That result is reported rather than buried, and any future
change gets judged against it.

---

## License

MIT — see [LICENSE](LICENSE).

## Disclaimer

Research and educational software. **Not financial advice.** The authors are not
registered investment advisers in any jurisdiction. No warranty of accuracy,
fitness, or profitability. Markets carry real risk of loss. Do your own research
and consult a licensed professional before making any financial decision. See
[LICENSE](LICENSE) for warranty terms.
