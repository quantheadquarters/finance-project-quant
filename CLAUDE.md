# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## How to communicate with Shubh (project owner)

- Start every response with the exact phrase: "okay shubh, ill do that"
- Shubh is learning to code. While working, explain what is going on in simple,
  plain terms — what each file is for, what a concept means, and why a decision
  was made — like teaching a beginner, not briefing an expert. Short
  "what just happened and why" notes beat jargon. Define technical terms the
  first time they appear.

## The cardinal rule (never violate)

Decision-bearing numbers (`direction`, `confidence`, `invalidation_level`, source
weights) come only from deterministic, tested pure Python. The LLM lives only in
`narrative/`, is optional and key-gated, and may write only the `thesis` prose —
never a number. No network calls or randomness in `analyzers/` or `synthesis/`.
The default path stays keyless. Never weaken the research-only disclaimer. If a
request would break this, flag it and propose the correct layer instead.

`execution/` (Dhan orders) is paper by default: a real order goes out only with
`LIVE_TRADING=1` **and** broker credentials present. Never make live the default,
and never let a request move money without that explicit gate.

## Commands

```bash
# System Python is externally managed (Homebrew) — always use the venv
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# A change is "done" only when all three pass:
pytest -q                                  # network-free suite
ruff check . && ruff format --check .      # CI gates lint AND format
python -m alpha_engine.cli.main scan BTC --no-record   # manual end-to-end check
#   --no-record matters: the signal log is a track record, and a developer
#   verifying a build should not append test scans to it.

pytest tests/test_core.py::test_name -q    # single test
pytest tests/test_execution.py -q          # the paper-first gate; run after ANY execution/ change
ruff format .                              # fix formatting
./start.sh <cmd>                           # zero-setup wrapper (venv + install + run)
```

## The shape of the thing

One-way pipeline under `src/alpha_engine/`; each stage may only look left:

```text
ingestion/ -> cache/ -> analyzers/ -> synthesis/ -> narrative/ -> Signal -> validation/
(network)    (local)   (pure fns)   (weighted vote) (prose only)          (append-only log)
                                                               `-> execution/ (paper-first, gated)
```

`schema/signal.py` is the contract everything compiles against. `web/` and `mcp.py` are
read-only transports and live *inside* the package, so a `pip install` ships the
dashboard, terminal, API and MCP server.
`.github/workflows/daily-signals.yml` runs the daily scan in the cloud (git scraping).

Three directories sit *beside* the pipeline, not in it: `quant/` (the 504-factor
registry, IC ranking, Black-Scholes), `orchestrator/` (headline-triggered
re-scans), and `strategy/` (user-written trading rules + the trade-level
backtester). All consume the pipeline; nothing in the pipeline imports them.

## The two backtesters answer different questions

Confusing them is the easiest mistake to make here.

- `validation/backtest.py` — *were the engine's own signals right?* Hit rate and
  calibration. No-lookahead is **structural**: `signal_at()` truncates the series
  before any analysis runs, so no caller can leak the future in.
- `strategy/engine.py` — *what would my own rule's account have done?* Trades,
  equity curve, Sharpe, drawdown. A user strategy gets the whole series, so
  no-lookahead can only be **detected**: the run re-executes the strategy on
  truncated history and reports bars whose signal changed. A non-empty
  `lookahead_violations` voids every metric in that report — always lead with it.

## The three outside surfaces

`toolkit.py` holds one tool table; MCP-over-stdio (`mcp_server.py`),
MCP-over-HTTP and REST (`web/server.py`) are thin transports over it. **Add a
tool there, not in a transport.** If it can write to disk, name its write
arguments in `WRITE_ARGS` so the AI terminal never sees them and the HTTP write
gate can refuse them.

The web app has two sections: `/dashboard` (read-only, no keys, no AI) and
`/terminal` (chat where an AI drives the tools using the *user's own* LLM key —
never stored, never logged). The terminal's real guarantee is not that the model
behaves; it is that every tool call and raw result is returned next to the prose,
so any number can be checked.

## Three silent footguns

These fail quietly — no crash, wrong behavior for months. Full list in AGENTS.md.

- **Writable paths go through `config.data_dir()`.** A hardcoded `Path("data/...")`
  is cwd-relative, so that module writes somewhere else than the rest of the engine.
- **Every new `ingestion/` adapter calls `alpha_engine.health.record`** with an item
  count, per *feed*. Adapters degrade to empty by design, so without it a dead source
  looks identical to a quiet one.
- **News / on-chain / fundamentals are cache-only in the scan path.** `_load_news`,
  `_load_onchain`, `_load_fundamentals` in `cli/main.py` never fetch; `ingest` and
  `orchestrate` populate them. Making them fetch inline takes `pytest` from ~23s to ~70s.

**Bugs go in a file, not in chat.** A defect you spot but do not fix goes in
[BUGS.md](BUGS.md) with its symptom, evidence, and proposed fix — never left as
a remark in conversation.

**Measured, not claimed:** [FINDINGS.md](FINDINGS.md) holds what the engine has
actually been shown to do, measured over thousands of logged signals. That file
is the only place the edge number lives — read it before trusting anything here,
and never restate the figure from memory.

## Everything else

Read [AGENTS.md](AGENTS.md) — it holds the full command list, architecture,
extension patterns, and gotchas. For deeper background, [context.md](context.md)
has the layer table; [FUTURE_WORK.md](FUTURE_WORK.md) holds the roadmap;
[README.md](README.md) has the full capability matrix.

The remaining top-level docs (`GETTING_STARTED.md`, `RUNNING_IT.md`,
`HOW_IT_WORKS.md`, `CONTRIBUTING.md`, `AUDIT.md`, `CHANGELOG.md`) are written for
humans and restate the above in longer form — skip them unless asked.
