# Your strategies go here

Every `.py` file in this folder is scanned for `BaseStrategy` subclasses when you
run `alpha-engine strategies` or `alpha-engine strategy-backtest`. There is no
registration step — drop a file in, and it shows up.

Set `ALPHA_STRATEGY_DIR` to point somewhere else (same override pattern as
`ALPHA_DATA_DIR`, and for the same reason: a scheduled job does not run from the
project root).

## The shape of one

```python
from alpha_engine.strategy.base import BaseStrategy
from alpha_engine.strategy.indicators import ema, crossed_above, crossed_below


class MyStrategy(BaseStrategy):
    name = "My Strategy"                       # shown in listings
    description = "EMA crossover."
    params = {"fast": 9, "slow": 21}           # tunable with --param fast=5

    def generate_signals(self, candles):
        """One signal per bar, same length and order as `candles`:
        1 = go long, -1 = go short, 0 = nothing."""
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

Read `src/alpha_engine/strategy/builtin/` for five complete candidate strategies:
SMA crossover, RSI reversal, Supertrend flip, Donchian breakout, and
momentum-with-volume. They are examples to test, not proven alpha. `RSIReversal`
also shows how to override `verify_on_option`.

Generate an interactive candle, entry-marker, and equity chart with:

```bash
./start.sh strategy-backtest AAPL --strategy SMACrossover --chart aapl.html
```

The report embeds its data in the HTML and uses TradingView Lightweight Charts
for display. Opening the report loads that display library from its CDN; it does
not fetch market data.

## Two things the engine does for you

**The fill is lagged.** A signal computed from bar `t`'s close is filled at
`t + 1`. You cannot accidentally buy at the price you just used to decide.

**Lookahead is checked.** Every backtest re-runs your `generate_signals` on
truncated history and reports any bar whose signal changed once the future was
removed. If `lookahead_violations` is non-empty, your strategy is reading bars it
would not have had — and every metric in that report is meaningless until you fix
it. This is the most common way a backtest lies, and it usually looks like a
brilliant result rather than a bug.

The usual cause is index arithmetic. Use the helpers in
`alpha_engine.strategy.indicators` — they return series the same length as your
candles, with `None` for the warm-up — so `fast[i]` always means bar `i` and you
never write an offset by hand.

## Indicators available

`sma`, `ema`, `rsi`, `atr`, `crossed_above`, `crossed_below`. Anything else you
need, compute inline — reading only `candles[:i + 1]` at bar `i`.

## A note on trust

These files are executed as Python when the engine loads them. That is fine
locally: it is the same trust level as running the repo at all. It is why **no
HTTP route in this project accepts strategy source** — a caller may select and
parameterise a strategy that is already on the server's disk, but putting code
here stays a local, filesystem-level act by whoever runs the server.
