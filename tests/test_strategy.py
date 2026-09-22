"""Tests for the user-strategy layer (the ported nifty_backtester).

The four properties worth pinning, in order of how much damage they do when
broken:

1. The lookahead detector actually catches a strategy that reads the future.
2. The position is lagged — a signal on bar t is filled at t+1, never at t.
3. Option confirmation can veto a signal, and it only ever sees past bars.
4. The metrics arithmetic matches a hand-computed case.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from alpha_engine.cache.models import Candle, Interval, PriceSeries
from alpha_engine.strategy.base import BaseStrategy
from alpha_engine.strategy.engine import align_option_to_underlying, run_strategy_backtest
from alpha_engine.strategy.indicators import crossed_above, ema, rsi, sma
from alpha_engine.strategy.loader import discover_strategies, load_strategy
from alpha_engine.strategy.metrics import compute_metrics, drawdown_series

START = datetime(2024, 1, 1, tzinfo=timezone.utc)


def _series(closes: list[float], asset: str = "TEST", volume: float | None = 1000.0) -> PriceSeries:
    return PriceSeries(
        asset=asset,
        interval=Interval.DAY,
        candles=[
            Candle(
                ts=START + timedelta(days=i),
                open=c,
                high=c * 1.01,
                low=c * 0.99,
                close=c,
                volume=volume,
            )
            for i, c in enumerate(closes)
        ],
    )


class AlwaysLong(BaseStrategy):
    name = "Always Long"
    params: dict = {}

    def generate_signals(self, candles):
        out = [0] * len(candles)
        if len(candles) > 1:
            out[1] = 1
        return out


class Peeker(BaseStrategy):
    """Deliberately cheats: goes long on bar t when bar t+1 closes higher."""

    name = "Peeker"
    params: dict = {}

    def generate_signals(self, candles):
        out = [0] * len(candles)
        for i in range(len(candles) - 1):
            out[i] = 1 if candles[i + 1].close > candles[i].close else -1
        return out


# --------------------------------------------------------------------------
# 1. The lookahead detector
# --------------------------------------------------------------------------


def test_lookahead_detector_catches_a_peeking_strategy():
    prices = [100 + (i % 7) * 3 for i in range(60)]
    report = run_strategy_backtest(Peeker(), _series(prices))
    assert report.lookahead_checked
    assert report.lookahead_violations, "a strategy reading candles[i+1] must be flagged"


def test_honest_strategy_reports_no_lookahead():
    prices = [100 + i * 0.5 for i in range(60)]
    report = run_strategy_backtest(load_strategy("SMACrossover"), _series(prices))
    assert report.lookahead_violations == []


@pytest.mark.parametrize(
    "name",
    ["SMACrossover", "RSIReversal", "SupertrendFlip", "DonchianBreakout", "MomentumVolume"],
)
def test_all_five_builtin_candidates_are_aligned_and_lookahead_clean(name):
    prices = [100.0 + i * 0.2 + (i % 11 - 5) * 1.5 for i in range(180)]
    series = _series(prices)
    strategy = load_strategy(name, directory="/nonexistent")
    signals = strategy.generate_signals(series.candles)
    report = run_strategy_backtest(strategy, series)

    assert len(signals) == len(series.candles)
    assert set(signals) <= {-1, 0, 1}
    assert report.lookahead_violations == []


def test_donchian_breakout_uses_the_prior_range_not_the_current_bar():
    series = _series([100.0] * 20 + [110.0])
    strategy = load_strategy("DonchianBreakout", directory="/nonexistent")

    assert strategy.generate_signals(series.candles)[-1] == 1


def test_momentum_volume_requires_current_volume_confirmation():
    closes = [100.0] * 20 + [110.0]
    quiet = _series(closes, volume=1000.0)
    loud = quiet.model_copy(
        update={
            "candles": quiet.candles[:-1]
            + [quiet.candles[-1].model_copy(update={"volume": 5000.0})]
        }
    )
    strategy = load_strategy("MomentumVolume", directory="/nonexistent")

    assert strategy.generate_signals(quiet.candles)[-1] == 0
    assert strategy.generate_signals(loud.candles)[-1] == 1


def test_chart_embeds_candles_signals_equity_and_required_attribution(tmp_path):
    from alpha_engine.strategy.chart import write_backtest_chart

    series = _series([100.0 + i for i in range(60)], asset="AAPL")
    report = run_strategy_backtest(AlwaysLong(), series, check_lookahead=False)
    path = write_backtest_chart(tmp_path / "aapl.html", series, report)
    page = path.read_text()

    assert "lightweight-charts@5.2.1" in page
    assert "AAPL" in page and "LONG" in page
    assert '"equity"' in page and '"candles"' in page
    assert "TradingView Lightweight Charts" in page
    chart_data = json.loads(page.split("const data=", 1)[1].split(";\nconst options", 1)[0])
    assert chart_data["markers"][0]["time"] == int(series.candles[2].ts.timestamp())


def test_lookahead_check_can_be_disabled():
    report = run_strategy_backtest(
        Peeker(), _series([100 + (i % 5) for i in range(40)]), check_lookahead=False
    )
    assert report.lookahead_checked is False
    assert report.lookahead_violations == []


# --------------------------------------------------------------------------
# 2. Fill lag — the signal's own bar must not be tradeable
# --------------------------------------------------------------------------


def test_position_is_lagged_one_bar():
    """Signal fires on bar 1. The return of bar 1 must NOT be captured; the
    first captured return starts at bar 2's open."""
    series = _series([100.0, 110.0, 121.0])
    series.candles[2] = series.candles[2].model_copy(update={"open": 110.0})
    report = run_strategy_backtest(AlwaysLong(), series, txn_cost_bps=0.0, check_lookahead=False)
    assert report.position == [0, 0, 1]
    # Bar 1's rise is missed; only bar 2's open-to-close rise is captured.
    assert report.equity_curve[1] == pytest.approx(100_000.0)
    assert report.equity_curve[2] == pytest.approx(110_000.0)
    assert report.trades[0].entry_price == 110.0


def test_next_open_fill_does_not_capture_prior_overnight_gap():
    report = run_strategy_backtest(
        AlwaysLong(), _series([100.0, 110.0, 121.0]), txn_cost_bps=0.0, check_lookahead=False
    )
    assert report.equity_curve[-1] == pytest.approx(100_000.0)
    assert report.trades[0].entry_price == 121.0


def test_transaction_cost_is_charged_on_position_change():
    free = run_strategy_backtest(
        AlwaysLong(), _series([100.0, 100.0, 100.0]), txn_cost_bps=0.0, check_lookahead=False
    )
    costly = run_strategy_backtest(
        AlwaysLong(), _series([100.0, 100.0, 100.0]), txn_cost_bps=100.0, check_lookahead=False
    )
    assert free.equity_curve[-1] == pytest.approx(100_000.0)
    assert costly.equity_curve[-1] < free.equity_curve[-1]


def test_backtest_rejects_fake_profit_from_negative_costs():
    with pytest.raises(ValueError, match="txn_cost_bps"):
        run_strategy_backtest(
            AlwaysLong(), _series([100.0] * 3), txn_cost_bps=-10, check_lookahead=False
        )


def test_short_account_cannot_revive_after_overnight_ruin():
    class AlwaysShort(BaseStrategy):
        name = "Always Short"
        params: dict = {}

        def generate_signals(self, candles):
            return [-1] + [0] * (len(candles) - 1)

    series = _series([100.0, 100.0, 900.0])
    series.candles[2] = series.candles[2].model_copy(update={"open": 300.0})
    report = run_strategy_backtest(AlwaysShort(), series, txn_cost_bps=0, check_lookahead=False)
    assert report.ruined_at_bar == 2
    assert report.metrics.total_return_pct == -100.0


# --------------------------------------------------------------------------
# 3. Option confirmation
# --------------------------------------------------------------------------


class VetoAll(BaseStrategy):
    name = "Veto All"
    params: dict = {}

    def generate_signals(self, candles):
        out = [0] * len(candles)
        if len(candles) > 2:
            out[2] = 1
        return out

    def verify_on_option(self, option_candles, t, signal):
        return False


def test_option_confirmation_can_veto_every_signal():
    under = _series([100.0 + i for i in range(20)])
    opt = _series([10.0 + i * 0.1 for i in range(20)], asset="TEST_CE")
    report = run_strategy_backtest(VetoAll(), under, opt, check_lookahead=False)
    assert report.signals_raw == 1
    assert report.signals_confirmed == 0
    assert set(report.position) == {0}
    assert report.trades == []


def test_verify_on_option_never_sees_future_bars():
    seen: list[int] = []

    class Recorder(BaseStrategy):
        name = "Recorder"
        params: dict = {}

        def generate_signals(self, candles):
            out = [0] * len(candles)
            out[10] = 1
            return out

        def verify_on_option(self, option_candles, t, signal):
            seen.append(len(option_candles))
            return True

    under = _series([100.0 + i for i in range(20)])
    opt = _series([10.0 + i for i in range(20)], asset="TEST_CE")
    run_strategy_backtest(Recorder(), under, opt, check_lookahead=False)
    assert seen == [11], "verification at bar 10 must see exactly bars 0..10"


def test_trade_on_option_uses_the_option_leg_for_pnl():
    under = _series([100.0, 100.0, 100.0, 100.0])  # underlying goes nowhere
    opt = _series([10.0, 10.0, 20.0, 20.0], asset="TEST_CE")  # option doubles
    opt.candles[2] = opt.candles[2].model_copy(update={"open": 10.0})
    report = run_strategy_backtest(
        AlwaysLong(),
        under,
        opt,
        trade_on="option",
        require_option_confirmation=False,
        txn_cost_bps=0.0,
        check_lookahead=False,
    )
    assert report.metrics.total_return_pct == pytest.approx(100.0)


def test_trade_on_option_without_option_series_is_rejected():
    with pytest.raises(ValueError, match="option price series"):
        run_strategy_backtest(AlwaysLong(), _series([1.0, 2.0, 3.0]), trade_on="option")


# --------------------------------------------------------------------------
# Option alignment
# --------------------------------------------------------------------------


def test_alignment_trims_to_the_overlap_and_forward_fills():
    under = _series([100.0 + i for i in range(10)])
    # Option starts 3 bars late and has a gap.
    opt_candles = [under.candles[3], under.candles[6]]
    opt = PriceSeries(asset="CE", interval=Interval.DAY, candles=opt_candles)

    trimmed, aligned = align_option_to_underlying(under.candles, opt.candles)
    assert len(trimmed) == len(aligned) == 7  # bars 3..9
    assert trimmed[0].ts == opt_candles[0].ts
    # Bars 4 and 5 forward-fill from bar 3 — never from bar 6 (that is the future).
    assert aligned[1] is opt_candles[0]
    assert aligned[2] is opt_candles[0]
    assert aligned[3] is opt_candles[1]


def test_alignment_with_no_option_returns_underlying_untouched():
    under = _series([1.0, 2.0, 3.0])
    trimmed, aligned = align_option_to_underlying(under.candles, [])
    assert trimmed == under.candles
    assert aligned == []


def test_sparse_option_quotes_cannot_be_filled_at_stale_prices():
    under = _series([100.0] * 5)
    sparse = PriceSeries(
        asset="CE", interval=Interval.DAY, candles=[under.candles[0], under.candles[3]]
    )
    with pytest.raises(ValueError, match="quote on every traded bar"):
        run_strategy_backtest(AlwaysLong(), under, sparse, trade_on="option", check_lookahead=False)


def test_stale_option_quote_cannot_confirm_a_signal():
    class SignalOnGap(BaseStrategy):
        name = "Signal On Gap"
        params: dict = {}

        def generate_signals(self, candles):
            return [0, 1, 0, 0]

        def verify_on_option(self, option_candles, t, signal):
            return True

    under = _series([100.0] * 4)
    sparse = PriceSeries(
        asset="CE", interval=Interval.DAY, candles=[under.candles[0], under.candles[2]]
    )
    report = run_strategy_backtest(SignalOnGap(), under, sparse, check_lookahead=False)
    assert report.signals_confirmed == 0
    assert report.metrics.total_return_pct == 0.0


# --------------------------------------------------------------------------
# 4. Metrics
# --------------------------------------------------------------------------


def test_drawdown_series_is_zero_on_a_monotonic_curve():
    assert drawdown_series([100.0, 110.0, 120.0]) == [0.0, 0.0, 0.0]


def test_drawdown_series_measures_from_the_running_peak():
    dd = drawdown_series([100.0, 200.0, 150.0])
    assert dd[2] == pytest.approx(-0.25)


def test_metrics_on_a_hand_computed_case():
    equity = [100.0, 110.0, 99.0]
    returns = [0.1, -0.1]
    metrics, dd = compute_metrics(equity, returns, [10.0, -11.0], bars_per_year=252.0)
    assert metrics.total_return_pct == pytest.approx(-1.0)
    assert metrics.max_drawdown_pct == pytest.approx(-10.0)
    assert metrics.trades == 2
    assert metrics.win_rate_pct == pytest.approx(50.0)
    assert metrics.profit_factor == pytest.approx(10.0 / 11.0, rel=1e-3)
    assert min(dd) == pytest.approx(-0.1)


def test_profit_factor_is_none_rather_than_infinity_when_nothing_lost():
    metrics, _ = compute_metrics([100.0, 120.0], [0.2], [20.0])
    assert metrics.profit_factor is None


def test_metrics_annualisation_follows_the_interval():
    daily = run_strategy_backtest(
        AlwaysLong(), _series([100.0 + i for i in range(30)]), check_lookahead=False
    )
    assert daily.metrics.bars_per_year == 252.0


# --------------------------------------------------------------------------
# Indicators
# --------------------------------------------------------------------------


def test_indicators_are_index_aligned_with_none_warmup():
    candles = _series([float(i) for i in range(1, 21)]).candles
    for series in (sma(candles, 5), ema(candles, 5), rsi(candles, 14)):
        assert len(series) == len(candles)
    assert sma(candles, 5)[:4] == [None] * 4
    assert sma(candles, 5)[4] == pytest.approx(3.0)  # mean of 1..5


def test_rsi_is_100_when_every_bar_rises():
    candles = _series([float(i) for i in range(1, 40)]).candles
    assert rsi(candles, 14)[-1] == pytest.approx(100.0)


def test_crossed_above_reads_only_current_and_previous_bar():
    fast = [None, 1.0, 3.0]
    slow = [None, 2.0, 2.0]
    assert crossed_above(fast, slow, 2) is True
    assert crossed_above(fast, slow, 1) is False  # previous bar is None -> undefined


# --------------------------------------------------------------------------
# Loader
# --------------------------------------------------------------------------


def test_builtin_strategies_are_discovered():
    strategies, errors = discover_strategies(directory="/nonexistent")
    assert "SMACrossover" in strategies
    assert "RSIReversal" in strategies
    assert errors == {}


def test_base_class_is_not_offered_as_a_strategy():
    strategies, _ = discover_strategies(directory="/nonexistent")
    assert "BaseStrategy" not in strategies


def test_unknown_strategy_error_lists_what_is_available():
    with pytest.raises(KeyError, match="SMACrossover"):
        load_strategy("NoSuchStrategy", directory="/nonexistent")


def test_unknown_parameter_is_rejected_at_construction():
    with pytest.raises(ValueError, match="fast_lenght"):
        load_strategy("SMACrossover", directory="/nonexistent", fast_lenght=5)


def test_a_broken_user_strategy_file_does_not_hide_the_working_ones(tmp_path):
    (tmp_path / "broken.py").write_text("this is not python(")
    (tmp_path / "good.py").write_text(
        "from alpha_engine.strategy.base import BaseStrategy\n"
        "class Good(BaseStrategy):\n"
        "    name = 'Good'\n"
        "    params = {}\n"
        "    def generate_signals(self, candles):\n"
        "        return [0] * len(candles)\n"
    )
    strategies, errors = discover_strategies(directory=tmp_path)
    assert "Good" in strategies
    assert "SMACrossover" in strategies  # built-ins still there
    assert "broken.py" in errors


def test_generate_signals_must_return_one_signal_per_bar():
    class Wrong(BaseStrategy):
        name = "Wrong"
        params: dict = {}

        def generate_signals(self, candles):
            return [0, 0]

    with pytest.raises(ValueError, match="one per bar"):
        run_strategy_backtest(Wrong(), _series([1.0, 2.0, 3.0, 4.0]), check_lookahead=False)


# --------------------------------------------------------------------------
# The built-in option-confirmation override
#
# `RSIReversal.verify_on_option` is the worked example of the one idea this
# layer has that the rest of the engine does not, and an audit found it at 31%
# coverage — i.e. the example nobody had checked.
# --------------------------------------------------------------------------


def _option_candles(closes: list[float], volumes: list[float]) -> list[Candle]:
    return [
        Candle(
            ts=START + timedelta(days=i),
            open=c,
            high=c,
            low=c,
            close=c,
            volume=v,
        )
        for i, (c, v) in enumerate(zip(closes, volumes))
    ]


def test_rsi_reversal_rejects_a_move_on_thin_volume():
    """Premium moving the right way on no volume is usually a stale quote or a
    single lot, not participation."""
    strategy = load_strategy("RSIReversal", directory="/nonexistent")
    rising = [10.0 + i for i in range(12)]
    thin = [1000.0] * 11 + [1.0]  # last bar far below its 10-bar mean
    assert strategy.verify_on_option(_option_candles(rising, thin), 11, 1) is False


def test_rsi_reversal_confirms_a_move_backed_by_volume():
    strategy = load_strategy("RSIReversal", directory="/nonexistent")
    rising = [10.0 + i for i in range(12)]
    heavy = [1000.0] * 11 + [5000.0]
    assert strategy.verify_on_option(_option_candles(rising, heavy), 11, 1) is True


def test_rsi_reversal_does_not_veto_when_volume_is_unreported():
    """A source that omits volume must not look like zero volume — missing data
    and no participation lead to opposite conclusions."""
    strategy = load_strategy("RSIReversal", directory="/nonexistent")
    candles = [
        Candle(ts=START + timedelta(days=i), open=c, high=c, low=c, close=c, volume=None)
        for i, c in enumerate(10.0 + i for i in range(12))
    ]
    assert strategy.verify_on_option(candles, 11, 1) is True


def test_rsi_reversal_defers_to_the_base_ema_check_first():
    """Volume cannot rescue a signal the base rule already rejected."""
    strategy = load_strategy("RSIReversal", directory="/nonexistent")
    falling = [30.0 - i for i in range(12)]  # well below its 5-EMA
    heavy = [1000.0] * 11 + [9999.0]
    assert strategy.verify_on_option(_option_candles(falling, heavy), 11, 1) is False


def test_rsi_reversal_generates_signals_at_the_oversold_exit():
    strategy = load_strategy("RSIReversal", directory="/nonexistent")
    # Drive RSI down hard, then reverse up so it crosses back above 30.
    closes = [100.0 - i * 2 for i in range(25)] + [55.0 + i * 3 for i in range(12)]
    signals = strategy.generate_signals(_series(closes).candles)
    assert len(signals) == len(closes)
    assert 1 in signals, "exiting oversold must produce a long signal"


def test_short_option_history_is_not_vetoed():
    """Fewer than 5 bars means no opinion, not a rejection."""
    strategy = load_strategy("RSIReversal", directory="/nonexistent")
    assert strategy.verify_on_option(_option_candles([1.0, 2.0], [1.0, 1.0]), 1, 1) is True
