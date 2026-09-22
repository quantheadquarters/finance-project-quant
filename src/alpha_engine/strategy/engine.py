"""Signal -> confirmation -> position -> trades -> equity curve.

The flow, in order:

1. `strategy.generate_signals(underlying)` returns one intent per bar (1/-1/0).
2. Every non-zero intent is offered to `strategy.verify_on_option(...)`. Only
   confirmed intents survive. This is the step the rest of the engine does not
   have, and the reason this module exists.
3. Surviving intents become a position, held until an opposite intent arrives.
4. P&L accrues on whichever leg the caller chose to trade, net of a flat
   transaction cost charged whenever the position changes.

Two honesty guards are built in rather than documented and hoped for:

- **The position is lagged one bar.** A signal computed from bar `t`'s close is
  filled at `t+1`, never at `t`. Paying the close you just used to decide is the
  oldest way to invent returns.
- **`check_lookahead`** re-runs the strategy on truncated history and reports any
  bar whose signal changed once the future was removed. Costs one extra pass
  over a sample of bars; catches the bug class that makes backtests lie.
"""

from __future__ import annotations

import math
from datetime import datetime

from pydantic import BaseModel, Field

from alpha_engine.cache.models import Candle, Interval, PriceSeries
from alpha_engine.strategy.base import BaseStrategy
from alpha_engine.strategy.metrics import (
    StrategyMetrics,
    bars_per_year_for,
    compute_metrics,
)

#: Bars sampled by the lookahead check. Re-running the strategy is O(n) per
#: sampled bar, so checking every bar would make the check quadratic; a spread
#: sample catches a systematic leak, which is the only kind that exists in
#: practice — nobody peeks at the future on exactly one bar.
_LOOKAHEAD_SAMPLES = 25


class Trade(BaseModel):
    """One round trip. `open` marks a position still held at the last bar — its
    P&L is marked-to-market, not realised."""

    entry_ts: datetime
    exit_ts: datetime
    direction: str  # "long" | "short"
    entry_price: float
    exit_price: float
    qty: float
    pnl: float
    return_pct: float
    open: bool = False


class StrategyBacktest(BaseModel):
    """The full result. Series fields are aligned to `timestamps`, so a chart
    can plot any of them against one x-axis with no further work."""

    strategy: str
    params: dict[str, float | int | str | bool]
    asset: str
    interval: Interval
    trade_on: str = Field(..., description="'underlying' or 'option' — which leg P&L accrued on")
    option_confirmation: bool

    bars: int
    timestamps: list[datetime]
    signals: list[int] = Field(..., description="raw strategy intent per bar")
    confirmed: list[bool] = Field(..., description="did the option chart confirm this bar's intent")
    position: list[int] = Field(..., description="position actually held, entering bar i")
    equity_curve: list[float]
    drawdown: list[float]

    signals_raw: int = Field(..., description="non-zero intents the strategy produced")
    signals_confirmed: int = Field(..., description="intents that survived option confirmation")
    trades: list[Trade]
    metrics: StrategyMetrics

    ruined_at_bar: int | None = Field(
        None,
        description="bar index where the account was wiped out, if it was. "
        "A compounding short can lose more than 100% in one bar; when that "
        "happens trading stops and every metric describes a dead account.",
    )

    lookahead_checked: bool = False
    lookahead_violations: list[int] = Field(
        default_factory=list,
        description="bar indices whose signal changed when future bars were removed; "
        "any entry here means the equity curve above is not trustworthy",
    )
    disclaimer: str = (
        "RESEARCH ONLY. A backtest is a measurement of the past under simplifying "
        "assumptions, not a prediction. Equity assumes full-capital exposure; "
        "individual trade P&L uses the fixed reporting quantity. Not financial advice."
    )


def align_option_to_underlying(
    underlying: list[Candle], option: list[Candle]
) -> tuple[list[Candle], list[Candle]]:
    """Trim both series to their overlapping range and forward-fill the option
    onto the underlying's bar index.

    Returns `(underlying_trimmed, option_aligned)` of equal length, where index
    `i` is the same moment in both. Forward-fill only ever reaches backwards to
    the most recent option bar at or before the underlying bar, so no future
    option price can leak in.

    Trimming to the overlap rather than padding is deliberate: an option that
    did not trade yet has no price, and inventing one is how a backtest starts
    lying before the first trade.
    """
    if not underlying or not option:
        return list(underlying), []

    start_ts = max(underlying[0].ts, option[0].ts)
    trimmed = [c for c in underlying if c.ts >= start_ts]
    if not trimmed:
        return [], []

    aligned: list[Candle] = []
    j = 0
    for candle in trimmed:
        while j + 1 < len(option) and option[j + 1].ts <= candle.ts:
            j += 1
        aligned.append(option[j])
    return trimmed, aligned


def _extract_trades(
    position: list[int],
    prices: list[float],
    timestamps: list[datetime],
    qty: float,
    txn_cost_bps: float,
    final_close: float,
) -> list[Trade]:
    """Use next-open execution prices; mark an unclosed trade at the final close."""
    trades: list[Trade] = []
    held = 0
    entry_i = 0

    def close(exit_i: int, still_open: bool) -> None:
        entry_price = prices[entry_i]
        exit_price = final_close if still_open else prices[exit_i]
        if entry_price <= 0:
            return
        gross = (exit_price - entry_price) * held * qty
        cost = txn_cost_bps / 10_000.0 * (entry_price + (0 if still_open else exit_price)) * qty
        pnl = gross - cost
        trades.append(
            Trade(
                entry_ts=timestamps[entry_i],
                exit_ts=timestamps[exit_i],
                direction="long" if held == 1 else "short",
                entry_price=round(entry_price, 4),
                exit_price=round(exit_price, 4),
                qty=qty,
                pnl=round(pnl, 2),
                return_pct=round((exit_price - entry_price) * held / entry_price * 100.0, 4),
                open=still_open,
            )
        )

    for i, pos in enumerate(position):
        if pos == held:
            continue
        if held != 0:
            close(i, still_open=False)
        if pos != 0:
            entry_i = i
        held = pos

    if held != 0:
        close(len(position) - 1, still_open=True)
    return trades


def _lookahead_violations(
    strategy: BaseStrategy, candles: list[Candle], signals: list[int]
) -> list[int]:
    """Re-run the strategy on truncated history and report bars whose signal
    changed. A non-empty result means `generate_signals` read future bars."""
    n = len(candles)
    if n < 3:
        return []
    # Skip the first 20% as warm-up: an indicator seeded differently on a short
    # slice is not lookahead, it is just an undefined indicator.
    start = max(2, n // 5)
    if start >= n:
        return []
    step = max(1, (n - start) // _LOOKAHEAD_SAMPLES)

    violations: list[int] = []
    for t in range(start, n, step):
        truncated = strategy.generate_signals(candles[: t + 1])
        if len(truncated) == t + 1 and truncated[t] != signals[t]:
            violations.append(t)
    return violations


def run_strategy_backtest(
    strategy: BaseStrategy,
    underlying: PriceSeries,
    option: PriceSeries | None = None,
    *,
    trade_on: str = "underlying",
    require_option_confirmation: bool = True,
    capital: float = 100_000.0,
    qty: float = 1.0,
    txn_cost_bps: float = 2.0,
    check_lookahead: bool = True,
) -> StrategyBacktest:
    """Backtest one strategy and return trades, equity curve and metrics.

    `trade_on="option"` computes P&L on the option leg while still generating
    signals from the underlying — how an options trader actually works: the
    index tells you the direction, the contract is what you own.
    """
    if trade_on not in ("underlying", "option"):
        raise ValueError("trade_on must be 'underlying' or 'option'")
    if not math.isfinite(capital) or capital <= 0:
        raise ValueError("capital must be finite and positive")
    if not math.isfinite(qty) or qty <= 0:
        raise ValueError("qty must be finite and positive")
    if not math.isfinite(txn_cost_bps) or txn_cost_bps < 0:
        raise ValueError("txn_cost_bps must be finite and nonnegative")

    option_candles_raw = option.candles if option else []
    candles, option_candles = align_option_to_underlying(underlying.candles, option_candles_raw)
    if trade_on == "option" and not option_candles:
        raise ValueError("trade_on='option' needs an option price series with overlapping bars")
    if trade_on == "option" and any(o.ts != c.ts for c, o in zip(candles, option_candles)):
        raise ValueError("trade_on='option' needs an option quote on every traded bar")
    if len(candles) < 2:
        raise ValueError("need at least 2 overlapping bars to backtest")

    confirm = require_option_confirmation and bool(option_candles)
    signals = strategy.generate_signals(candles)
    if len(signals) != len(candles):
        raise ValueError(
            f"{type(strategy).__name__}.generate_signals returned {len(signals)} signals "
            f"for {len(candles)} candles; it must return exactly one per bar"
        )

    confirmed: list[bool] = []
    for t, sig in enumerate(signals):
        confirmed.append(
            bool(sig)
            and (
                option_candles[t].ts == candles[t].ts
                and strategy.verify_on_option(option_candles[: t + 1], t, sig)
                if confirm
                else True
            )
        )

    # A close-of-bar intent becomes a held position only at the next bar's open.
    target_position: list[int] = []
    held = 0
    for sig, ok in zip(signals, confirmed):
        if sig != 0 and ok:
            held = sig
        target_position.append(held)
    position = [0, *target_position[:-1]]

    leg = option_candles if trade_on == "option" else candles
    prices = [c.open for c in leg]
    timestamps = [c.ts for c in candles]

    returns: list[float] = [0.0]
    equity: list[float] = [capital]
    ruined_at: int | None = None
    for i in range(1, len(prices)):
        prev_close = leg[i - 1].close
        today_open = leg[i].open
        today_close = leg[i].close
        if not all(math.isfinite(v) and v > 0 for v in (prev_close, today_open, today_close)):
            raise ValueError("backtest needs positive finite open and close prices")
        # The old position owns the overnight gap. The new position starts at
        # today's open, after its entry/exit fee, and owns only today's session.
        overnight = 1 + position[i - 1] * (today_open / prev_close - 1)
        turnover = abs(position[i] - position[i - 1])
        after_fee = 1 - turnover * txn_cost_bps / 10_000.0
        session = 1 + position[i] * (today_close / today_open - 1)
        net = overnight * after_fee * session - 1

        # RUIN. An account cannot lose more than everything, and a compounding
        # model will happily let it: a short position (-1) through a bar that
        # rises 300% gives net = -3.0, so equity *= (1 - 3.0) and the curve goes
        # NEGATIVE. Every metric downstream then reports nonsense — a real run
        # on option data produced "total return +609%" beside "max drawdown
        # -293%" and a negative Sharpe, which is not a bad result, it is not a
        # result at all.
        #
        # Options make this ordinary rather than exotic: a premium routinely
        # doubles in a session, and this engine is built to trade the option leg.
        #
        # So the account is wiped out at zero and trading stops. That is what
        # would actually happen, and it makes the metrics honest: -100% return,
        # -100% drawdown, and `ruined_at_bar` naming the bar it happened on.
        if ruined_at is not None:
            returns.append(0.0)
            equity.append(0.0)
            continue
        if min(overnight, after_fee, session) <= 0.0:
            ruined_at = i
            returns.append(-1.0)
            equity.append(0.0)
            continue

        returns.append(net)
        equity.append(equity[-1] * (1.0 + net))

    if ruined_at is not None:
        # Nothing is held after ruin, so no trade may be reported as still open.
        position = position[: ruined_at + 1] + [0] * (len(position) - ruined_at - 1)

    trades = _extract_trades(position, prices, timestamps, qty, txn_cost_bps, leg[-1].close)
    metrics, drawdown = compute_metrics(
        equity,
        returns[1:],
        [t.pnl for t in trades],
        bars_per_year=bars_per_year_for(underlying.interval),
    )

    violations = _lookahead_violations(strategy, candles, signals) if check_lookahead else []

    return StrategyBacktest(
        strategy=strategy.name,
        params={k: v for k, v in strategy.params.items() if isinstance(v, (int, float, str, bool))},
        asset=underlying.asset,
        interval=underlying.interval,
        trade_on=trade_on,
        option_confirmation=confirm,
        bars=len(candles),
        timestamps=timestamps,
        signals=signals,
        confirmed=confirmed,
        position=position,
        equity_curve=[round(e, 2) for e in equity],
        drawdown=[round(d, 6) for d in drawdown],
        signals_raw=sum(1 for s in signals if s != 0),
        signals_confirmed=sum(1 for ok in confirmed if ok),
        trades=trades,
        metrics=metrics,
        ruined_at_bar=ruined_at,
        lookahead_checked=check_lookahead,
        lookahead_violations=violations,
    )
