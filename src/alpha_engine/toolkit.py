"""The engine's callable surface: one tool table, many transports.

Three things now expose this engine to the outside world — the MCP server
(`mcp_server.py`), the HTTP API (`web/server.py`), and the AI terminal
(`narrative/agent.py`). Each used to be a plausible place to define "what can a
caller ask for", and three definitions of that is three chances to drift: a tool
fixed in one, a schema tightened in another, a disclaimer added to only one.

So the tool table lives here, in the library, and the transports are thin. A new
tool is added once and appears on all three surfaces at the same moment.

The four non-negotiables (from FUTURE_WORK Phase 14) bind every caller, and are
enforced here rather than per-transport so no transport can skip one:

1. **The disclaimer travels with every payload.** Results get pasted into other
   people's contexts; the research-only framing must be inseparable from the data.
2. **Cache-first, hard.** Every tool defaults to `no_refresh=True`. A public
   surface that refetches per call gets the host IP banned by CoinGecko in a day.
3. **Read-only by default.** Nothing writes to the signal log unless asked. The
   log is the compounding asset; an exploratory assistant must not pollute it.
4. **No tool accepts a number that becomes a decision.** There is no
   `set_confidence`, no `override_weight`. Tools answer questions; they do not
   accept opinions.

A fifth, added when the strategy layer landed:

5. **No tool accepts code.** `strategy_backtest` takes the *name* of a strategy
   already on the server's disk. Accepting source would be remote code
   execution. See `strategy/loader.py` for the full reasoning.
"""

from __future__ import annotations

import json
from typing import Any, Callable

DISCLAIMER = (
    "RESEARCH ONLY. This is not financial advice, not a recommendation, and not "
    "a solicitation to trade. Signals are the output of deterministic statistical "
    "models with no proven edge. Past behaviour does not predict future results. "
    "Anyone acting on this is doing so entirely at their own risk."
)

INSTRUCTIONS = (
    "Deterministic quant research engine. Every number these tools return was "
    "computed by tested Python, never by a language model. Relay the results; do "
    "not recompute, extrapolate, or estimate them. If you need a number you do "
    "not have, call a tool for it — do not guess. " + DISCLAIMER
)


# ---------------------------------------------------------------------------
# Helpers shared by every handler
# ---------------------------------------------------------------------------


def _cache():
    from alpha_engine.cache.interface import Cache

    return Cache()


def _resolve(asset: str, market: str | None):
    from alpha_engine.cli.main import detect_market

    return asset.upper(), detect_market(asset, market)


def _series_or_error(asset: str, market, days: int):
    """Cache-only price load. Returns `(series, None)` or `(None, error_dict)`."""
    from alpha_engine.cli.main import _load_series

    series = _load_series(asset, market, days, True, _cache())
    if not series.candles:
        return None, {
            "error": f"no cached data for {asset}",
            "hint": f"run `alpha-engine scan {asset}` once to populate the cache",
        }
    return series, None


def _dump(model) -> dict[str, Any]:
    """Pydantic model -> plain JSON dict (datetimes become strings)."""
    return json.loads(model.model_dump_json())


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def tool_scan(args: dict[str, Any]) -> dict[str, Any]:
    from alpha_engine.cli.main import _build_price_signal, _load_series
    from alpha_engine.schema.signal import Market

    asset, market = _resolve(args["asset"], args.get("market"))
    cache = _cache()

    if market is Market.IN_FNO:
        from alpha_engine.cli.main import _build_fno_signal

        chain, _stale = cache.get_chain(asset)
        if chain is None:
            return {"error": f"no options chain cached for {asset}; run `fetch-chain` first"}
        signal = _build_fno_signal(asset, chain)
    else:
        series = _load_series(asset, market, args.get("days", 90), True, cache)
        if not series.candles:
            return {"error": f"no cached data for {asset}; run `scan {asset}` in the CLI first"}
        signal = _build_price_signal(asset, market, series, cache, no_refresh=True)

    # Writing to the log is opt-in. The log is the compounding asset.
    if args.get("record"):
        from alpha_engine.validation.recorder import record_signal

        record_signal(signal)

    return _dump(signal)


def tool_report(args: dict[str, Any]) -> dict[str, Any]:
    from alpha_engine.quant.report import build_report

    asset, market = _resolve(args["asset"], args.get("market"))
    series, error = _series_or_error(asset, market, args.get("days", 180))
    if error:
        return error
    return _dump(build_report(series, market.value))


def tool_backtest(args: dict[str, Any]) -> dict[str, Any]:
    from alpha_engine.validation.backtest import run_backtest

    asset, market = _resolve(args["asset"], args.get("market"))
    series, error = _series_or_error(asset, market, args.get("days", 365))
    if error:
        return error
    return _dump(run_backtest(series, market, step=args.get("step", 5)))


def tool_options_backtest(args: dict[str, Any]) -> dict[str, Any]:
    from alpha_engine.validation.options_backtest import run_options_backtest

    asset, market = _resolve(args["asset"], args.get("market"))
    series, error = _series_or_error(asset, market, args.get("days", 365))
    if error:
        return error
    report = run_options_backtest(
        series,
        market=market,
        step=args.get("step", 5),
        dte_bars=args.get("dte_bars", 21),
    )
    payload = _dump(report)
    payload["pricing_note"] = (
        "The underlying P&L is real (cached candles). The option P&L is MODEL-PRICED "
        "with Black-Scholes from a trailing realized-vol estimate — no IV smile, no "
        "bid/ask, no skew. Read it as 'what a textbook option would have done', not "
        "'what you would have filled'."
    )
    return payload


def tool_factors(args: dict[str, Any]) -> dict[str, Any]:
    from alpha_engine.quant.factors import FACTOR_REGISTRY, compute_panel, factor_names
    from alpha_engine.quant.ranking import noise_floor_ic, rank_factors

    asset, market = _resolve(args["asset"], args.get("market"))
    series, error = _series_or_error(asset, market, args.get("days", 365))
    if error:
        return error

    family = args.get("family")
    names = factor_names(families=[family] if family else None)
    if not names:
        return {"error": f"unknown factor family '{family}'"}

    panel = compute_panel(series, names=names)
    scores = rank_factors(series, panel, horizon=args.get("horizon", 10))

    obs = [s.n_obs for s in scores if s.n_obs > 0]
    median_obs = sorted(obs)[len(obs) // 2] if obs else 0
    floor = noise_floor_ic(len(scores), median_obs)

    # Per-factor floor: coverage varies across the registry and the floor scales
    # with 1/sqrt(n), so the median-derived line above is too lenient for the
    # long-window factors that crowd the top. See cli/main.py for the long note.
    own_floor = {s.name: noise_floor_ic(len(scores), s.n_obs) for s in scores}

    top = args.get("top", 25)
    return {
        "asset": asset,
        "bars": len(series.candles),
        "factors_scored": len(scores),
        "noise_floor_ic": round(floor, 4) if floor else None,
        "noise_floor_note": (
            "An |IC| below the noise floor is what the best of this many purely "
            "random factors would reach by chance. Below it means nothing. The "
            "top-level figure uses the median factor's sample size; judge each "
            "factor by its own clears_own_noise_floor, which accounts for how "
            "much data that factor actually had."
        ),
        "factors": [
            {
                "factor": s.name,
                "family": FACTOR_REGISTRY[s.name].family if s.name in FACTOR_REGISTRY else None,
                "rank_ic": s.rank_ic,
                "hit_rate": s.hit_rate,
                "coverage": round(s.coverage, 3),
                "n_obs": s.n_obs,
                "own_noise_floor_ic": (
                    round(own_floor[s.name], 4) if own_floor.get(s.name) is not None else None
                ),
                "clears_own_noise_floor": (
                    None
                    if own_floor.get(s.name) is None or s.rank_ic is None
                    else abs(s.rank_ic) >= own_floor[s.name]
                ),
            }
            for s in scores[:top]
        ],
    }


def tool_record_stats(args: dict[str, Any]) -> dict[str, Any]:
    """The live track record — the honest answer to 'does it work?'"""
    from alpha_engine.validation.outcomes import (
        annotate_coverage,
        score_record,
        summarize_outcomes,
    )
    from alpha_engine.validation.recorder import read_records

    records = read_records()
    if not records:
        return {"records": 0, "note": "no signals recorded yet"}

    cache = _cache()
    scored = []
    missing_assets: set[str] = set()
    for record in records:
        series, _stale = cache.get_price(record.signal.asset, "1d")
        if series is None:
            missing_assets.add(record.signal.asset)
            continue
        scored.append((record.signal.confidence, score_record(record, series)))

    # Same coverage rule as the CLI. This is the surface an AI reads over MCP,
    # so an unqualified hit rate here is the one most likely to be quoted.
    return annotate_coverage(
        _dump(summarize_outcomes(scored)),
        total=len(records),
        scored=len(scored),
        missing_assets=missing_assets,
    )


def tool_health(args: dict[str, Any]) -> dict[str, Any]:
    """Per-source freshness. Lets a caller tell 'this source says nothing today'
    from 'this source died three weeks ago' — the whole point of health records."""
    from alpha_engine.health import load_health

    return load_health().summary()


def tool_list_strategies(args: dict[str, Any]) -> dict[str, Any]:
    from alpha_engine.strategy.loader import list_strategies

    payload = list_strategies()
    payload["note"] = (
        "Strategies are Python files on the server's disk. This tool lists and "
        "parameterises them; it cannot accept new code — that would be remote "
        "code execution. Add a strategy by putting a file in the strategy folder."
    )
    return payload


def tool_strategy_backtest(args: dict[str, Any]) -> dict[str, Any]:
    """Trade-level backtest of a named strategy: equity curve, trades, Sharpe.

    Distinct from `backtest`, which scores the *engine's own* signals for hit
    rate. This one asks what the money would have done.
    """
    from alpha_engine.strategy.engine import run_strategy_backtest
    from alpha_engine.strategy.loader import load_strategy

    asset, market = _resolve(args["asset"], args.get("market"))
    days = args.get("days", 365)
    series, error = _series_or_error(asset, market, days)
    if error:
        return error

    option_series = None
    option_asset = args.get("option_asset")
    if option_asset:
        option_asset_u, option_market = _resolve(option_asset, args.get("market"))
        option_series, opt_error = _series_or_error(option_asset_u, option_market, days)
        if opt_error:
            return opt_error

    params = args.get("params") or {}
    if not isinstance(params, dict):
        return {"error": "params must be an object of parameter name -> value"}

    try:
        strategy = load_strategy(args["strategy"], **params)
    except (KeyError, ValueError) as e:
        return {"error": str(e).strip("'\"")}

    try:
        report = run_strategy_backtest(
            strategy,
            series,
            option_series,
            trade_on=args.get("trade_on", "underlying"),
            require_option_confirmation=args.get("require_option_confirmation", True),
            capital=args.get("capital", 100_000.0),
            txn_cost_bps=args.get("txn_cost_bps", 2.0),
        )
    except ValueError as e:
        return {"error": str(e)}

    payload = _dump(report)
    if report.ruined_at_bar is not None:
        payload["warning"] = (
            f"ACCOUNT WIPED OUT at bar {report.ruined_at_bar}. A compounding "
            "position lost more than 100% in one bar and trading stopped there. "
            "Every metric describes a dead account."
        )
    if report.lookahead_violations:
        payload["warning"] = (
            f"LOOKAHEAD DETECTED on {len(report.lookahead_violations)} sampled bar(s). "
            "This strategy's signals change when future bars are removed, so the "
            "equity curve and every metric above are meaningless. Fix the strategy "
            "before reading any of these numbers."
        )
    # Series are large and rarely what a language model needs; keep them behind a flag
    # so a chat transcript is not flooded with 4,000 floats.
    if not args.get("include_series"):
        for key in ("timestamps", "signals", "confirmed", "position", "equity_curve", "drawdown"):
            payload.pop(key, None)
        payload["series_note"] = "Pass include_series=true for the per-bar arrays."
    return payload


# ---------------------------------------------------------------------------
# The tool table
# ---------------------------------------------------------------------------

_ASSET = {"type": "string", "description": "Ticker, e.g. BTC, AAPL, RELIANCE.NS, NIFTY"}
_MARKET = {
    "type": "string",
    "enum": ["crypto", "us_equity", "in_equity", "in_fno", "forex"],
    "description": "Override auto-detection",
}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "scan",
        "description": (
            "Generate a research signal for one asset: direction, calibrated "
            "confidence, invalidation level, and every contributing source with "
            "its weight. Serves cached data by default."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "asset": _ASSET,
                "market": _MARKET,
                "days": {"type": "integer", "description": "History window (default 90)"},
                "record": {
                    "type": "boolean",
                    "description": "Append to the signal log. Default false: the log is a "
                    "track record, not a scratchpad.",
                },
            },
            "required": ["asset"],
        },
    },
    {
        "name": "report",
        "description": (
            "Full quantitative report for one asset: trend, momentum, volatility "
            "regime, volume structure, and model reads (Kalman/GARCH/HMM)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "asset": _ASSET,
                "market": _MARKET,
                "days": {"type": "integer", "description": "History window (default 180)"},
            },
            "required": ["asset"],
        },
    },
    {
        "name": "backtest",
        "description": (
            "Replay history through the analyzer pipeline with no lookahead and "
            "report hit rate, average captured move, and calibration. This scores "
            "the ENGINE'S signals; use strategy_backtest for trade-level P&L."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "asset": _ASSET,
                "market": _MARKET,
                "days": {"type": "integer", "description": "History to replay (default 365)"},
                "step": {"type": "integer", "description": "Bars between signals (default 5)"},
            },
            "required": ["asset"],
        },
    },
    {
        "name": "options_backtest",
        "description": (
            "Replay the same no-lookahead signals but simulate buying the matching "
            "at-the-money option, and report option vs underlying returns side by "
            "side. Shows leverage and theta decay. The option leg is model-priced, "
            "not filled prices."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "asset": _ASSET,
                "market": _MARKET,
                "days": {"type": "integer", "description": "History to replay (default 365)"},
                "step": {"type": "integer", "description": "Bars between signals (default 5)"},
                "dte_bars": {
                    "type": "integer",
                    "description": "Days to expiry on the simulated option (default 21)",
                },
            },
            "required": ["asset"],
        },
    },
    {
        "name": "factors",
        "description": (
            "Rank 500+ deterministic factors by measured predictive power (rank "
            "IC) for one asset. Includes the multiple-testing noise floor, which "
            "says what the best purely random factor would have scored. Never "
            "present a top factor without comparing it to that floor."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "asset": _ASSET,
                "market": _MARKET,
                "days": {"type": "integer", "description": "History window (default 365)"},
                "horizon": {"type": "integer", "description": "Forward return bars (default 10)"},
                "family": {"type": "string", "description": "Restrict to one factor family"},
                "top": {"type": "integer", "description": "Rows to return (default 25)"},
            },
            "required": ["asset"],
        },
    },
    {
        "name": "record_stats",
        "description": (
            "The live track record: how recorded signals actually resolved. "
            "Read-only. This is the honest answer to 'does it work?'"
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "health",
        "description": (
            "Per-source data health: last success, item counts, consecutive errors. "
            "Use this when a result looks empty or stale — it distinguishes a quiet "
            "source from a dead one."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_strategies",
        "description": (
            "List the trading strategies available on this server and their tunable "
            "parameters. Call this before strategy_backtest to learn valid names."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "strategy_backtest",
        "description": (
            "Backtest one named strategy at the TRADE level: entries, exits, "
            "transaction costs, equity curve, Sharpe/Sortino/Calmar, max drawdown, "
            "win rate. Optionally cross-verify every signal against a real option "
            "price series before it becomes a trade. Reports any lookahead it "
            "detects — if lookahead_violations is non-empty, the numbers are void."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "asset": _ASSET,
                "strategy": {
                    "type": "string",
                    "description": "Strategy class name from list_strategies, e.g. SMACrossover",
                },
                "market": _MARKET,
                "days": {"type": "integer", "description": "History window (default 365)"},
                "params": {
                    "type": "object",
                    "description": 'Strategy parameter overrides, e.g. {"fast_length": 5}',
                },
                "option_asset": {
                    "type": "string",
                    "description": "Ticker of an option series to cross-verify signals against",
                },
                "trade_on": {
                    "type": "string",
                    "enum": ["underlying", "option"],
                    "description": "Which leg P&L accrues on (default underlying)",
                },
                "require_option_confirmation": {
                    "type": "boolean",
                    "description": "Only trade signals the option chart confirms (default true)",
                },
                "capital": {"type": "number", "description": "Starting capital (default 100000)"},
                "txn_cost_bps": {
                    "type": "number",
                    "description": "Round-trip cost in basis points (default 2)",
                },
                "include_series": {
                    "type": "boolean",
                    "description": "Include per-bar arrays (equity curve, signals). Default false.",
                },
            },
            "required": ["asset", "strategy"],
        },
    },
]

HANDLERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "scan": tool_scan,
    "report": tool_report,
    "backtest": tool_backtest,
    "options_backtest": tool_options_backtest,
    "factors": tool_factors,
    "record_stats": tool_record_stats,
    "health": tool_health,
    "list_strategies": tool_list_strategies,
    "strategy_backtest": tool_strategy_backtest,
}

# The only arguments in this whole surface that change state on disk. Named per
# tool rather than as a flag, so `read_only_tools()` can strip exactly these and
# `call_tool(read_only=True)` can refuse exactly these — no caller has to
# remember which tool is the dangerous one.
WRITE_ARGS: dict[str, set[str]] = {"scan": {"record"}}

#: Tools that can change state at all, given the right argument.
WRITE_CAPABLE = set(WRITE_ARGS)


# Numeric bounds for every tool argument that has one, checked before dispatch.
#
# This is not defensive padding — it closes a real hole. Without it the API
# answered `step=-5` with HTTP 200 and a backtest reporting zero signals, and
# `capital=-1000` with a full metrics block computed off a negative equity
# curve. Both are *confidently wrong numbers*, which in this project is a worse
# outcome than an error: the whole premise is that a number you can read is a
# number you can trust.
#
# `range(warmup, n, -5)` is empty rather than an exception, so nothing raised
# and nothing looked broken. That is exactly the class of failure the engine is
# built to make loud.
#
# Bounds are generous — they reject the meaningless, not the unusual. Upper
# limits double as the CPU guard on a public surface, since `factors` and
# `backtest` are seconds of work each.
_BOUNDS: dict[str, tuple[float, float]] = {
    "days": (1, 3650),  # ten years; beyond this no free source has history
    "step": (1, 10_000),  # 0 raises, negatives silently produce nothing
    "horizon": (1, 500),  # forward-return window, in bars
    "top": (1, 1000),  # rows returned from the factor ranking
    "dte_bars": (1, 365),  # days to expiry on the simulated option
    "capital": (0.01, 1e12),  # a negative account is not a backtest
    "txn_cost_bps": (0, 10_000),  # 10_000 bps = 100%, already absurd
    "max_steps": (1, 20),  # agent tool-calling rounds
}


def validate_args(name: str, args: dict[str, Any]) -> str | None:
    """Return an error message if any argument is out of bounds, else None.

    Runs in `call_tool`, so every transport inherits it — the MCP stdio server,
    the HTTP API and the AI terminal cannot disagree about what is acceptable.
    """
    for key, value in args.items():
        bounds = _BOUNDS.get(key)
        if bounds is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return f"{name}: '{key}' must be a number, got {type(value).__name__}"
        low, high = bounds
        if not (low <= value <= high):
            return f"{name}: '{key}'={value} is out of range [{low:g}, {high:g}]"
    return None


def tool_names() -> list[str]:
    return [t["name"] for t in TOOLS]


def read_only_tools() -> list[dict[str, Any]]:
    """The tool table with every write-capable argument removed.

    This is what the AI terminal advertises to a language model. Stripping the
    argument from the *schema* rather than only refusing it at call time matters:
    a model shown a `record` parameter will eventually decide that recording its
    findings is helpful, and the cleanest way to prevent that is for the
    parameter never to exist in the model's view of the world.
    """
    stripped: list[dict[str, Any]] = []
    for tool in TOOLS:
        blocked = WRITE_ARGS.get(tool["name"])
        if not blocked:
            stripped.append(tool)
            continue
        schema = dict(tool["inputSchema"])
        schema["properties"] = {
            k: v for k, v in schema.get("properties", {}).items() if k not in blocked
        }
        stripped.append({**tool, "inputSchema": schema})
    return stripped


def call_tool(
    name: str, args: dict[str, Any] | None = None, read_only: bool = False
) -> dict[str, Any]:
    """Run one tool and attach the disclaimer.

    `read_only=True` drops any write-capable argument before dispatch. It is a
    belt-and-braces pair with `read_only_tools()`: the schema hides the argument,
    and this refuses it even if a caller sends it anyway.

    Errors come back as data, never as exceptions: a tool failure must be
    something the caller can read and relay, not something that kills the
    transport it arrived on.
    """
    handler = HANDLERS.get(name)
    if handler is None:
        return {
            "error": f"unknown tool '{name}'",
            "available": tool_names(),
            "disclaimer": DISCLAIMER,
        }

    args = dict(args or {})
    if read_only:
        for blocked in WRITE_ARGS.get(name, set()):
            args.pop(blocked, None)

    bounds_error = validate_args(name, args)
    if bounds_error:
        return {"error": bounds_error, "disclaimer": DISCLAIMER}

    try:
        result = handler(args)
    except KeyError as e:  # a required argument was missing
        result = {"error": f"missing required argument: {e}"}
    except Exception as e:  # noqa: BLE001 - a tool failure is a result, not a crash
        result = {"error": f"{type(e).__name__}: {e}"}
    if not isinstance(result, dict):
        result = {"result": result}
    result["disclaimer"] = DISCLAIMER
    return result
