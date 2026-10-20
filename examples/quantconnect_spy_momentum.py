"""Frozen price-only momentum candidate on historical SPY members.

Paste this whole file into QuantConnect Cloud's main.py. Research only.
This is a broad-universe diagnostic, not a promoted Alpha Engine signal.
"""

# ruff: noqa: F403, F405 -- QuantConnect requires its generated star-import module.

from datetime import date, timedelta

from AlgorithmImports import *


def rank_closes(closes_by_symbol):
    """Same 252-to-21 momentum and 200-close filter as the local candidate."""
    ranked = []
    for symbol, closes in closes_by_symbol.items():
        if len(closes) < 253 or closes[-253] <= 0:
            continue
        if closes[-1] <= sum(closes[-200:]) / 200:
            continue
        score = closes[-22] / closes[-253] - 1
        if score > 0:
            ranked.append((score, symbol))
    ranked.sort(key=lambda pair: (pair[0], pair[1].value), reverse=True)
    return [symbol for _, symbol in ranked[:2]]


class TenBpsFeeModel(FeeModel):
    def get_order_fee(self, parameters):
        amount = abs(parameters.order.quantity) * parameters.security.price * 0.001
        return OrderFee(CashAmount(amount, "USD"))


class HistoricalSPYMomentum(QCAlgorithm):
    def initialize(self):
        self.set_start_date(2015, 1, 2)
        self.set_end_date(2026, 9, 21)  # The local candidate's holdout begins the next day.
        self.set_cash(100_000)
        self.universe_settings.resolution = Resolution.DAILY
        self.universe_settings.data_normalization_mode = DataNormalizationMode.RAW
        self._spy = self.add_equity(
            "SPY", Resolution.DAILY, data_normalization_mode=DataNormalizationMode.RAW
        ).symbol
        self.set_benchmark(self._spy)
        self._members = set()
        self._decision_members = None
        self._last_month = None
        self._rebalances = 0
        self.add_universe(self.universe.etf("SPY", universe_filter_func=self._select_members))
        self.schedule.on(
            self.date_rules.every_day(self._spy),
            self.time_rules.before_market_open(self._spy, 10),
            self._rebalance,
        )
        self.debug("RESEARCH ONLY: frozen price-only rule; no demonstrated alpha")

    def _select_members(self, constituents):
        self._members = {item.symbol for item in constituents}
        return list(self._members)

    def on_securities_changed(self, changes):
        for security in changes.added_securities:
            security.set_fee_model(TenBpsFeeModel())

    def on_data(self, data):
        # Daily SPY bars arrive after the close. Freeze that day's membership;
        # the next morning uses only prices available by this close.
        for symbol, delisting in data.delistings.items():
            if delisting.type == DelistingType.WARNING and self.portfolio[symbol].quantity:
                self.market_on_open_order(symbol, -self.portfolio[symbol].quantity)
        if self._spy not in data.bars:
            return
        month = (self.time.year, self.time.month)
        if month != self._last_month:
            self._last_month = month
            self._decision_members = set(self._members)

    def _rebalance(self):
        if self.time.date() == date(2026, 9, 21):
            for holding in self.portfolio.values():
                if holding.invested:
                    self.market_on_open_order(holding.symbol, -holding.quantity)
            self.debug("Final liquidation at the last pre-holdout open")
            return
        if self._decision_members is None:
            return
        members = sorted(self._decision_members, key=lambda symbol: symbol.value)
        self._decision_members = None
        if not members:
            self.error("No dated SPY membership at the monthly decision")
            return
        history = self.history(
            symbols=members,
            start=self.time - timedelta(days=430),
            end=self.time,
            resolution=Resolution.DAILY,
            fill_forward=False,
            data_normalization_mode=DataNormalizationMode.SCALED_RAW,
        )
        if history.empty:
            self.error("No point-in-time price history for SPY members")
            return
        closes = {}
        for symbol in members:
            try:
                values = history.loc[symbol]["close"].tail(253).tolist()
            except KeyError:
                continue
            if len(values) == 253 and self.securities[symbol].is_tradable:
                closes[symbol] = values
        selected = rank_closes(closes)
        self._rebalances += 1
        self.debug(
            f"{self.time.date()} members={len(members)} history={len(closes)} "
            f"picks={','.join(symbol.value for symbol in selected) or 'cash'}"
        )

        # Sell before buying; all orders target this morning's opening auction.
        for holding in self.portfolio.values():
            if holding.invested and holding.symbol not in selected:
                self.market_on_open_order(holding.symbol, -holding.quantity)
        for symbol in selected:
            quantity = self.calculate_order_quantity(symbol, 0.5)
            if quantity < 0:
                self.market_on_open_order(symbol, quantity)
        for symbol in selected:
            quantity = self.calculate_order_quantity(symbol, 0.5)
            if quantity > 0:
                self.market_on_open_order(symbol, quantity)

    def on_order_event(self, event):
        if event.status in (OrderStatus.INVALID, OrderStatus.CANCELED):
            self.error(f"ORDER {event.status}: {event.message}")

    def on_end_of_algorithm(self):
        self.debug(f"Monthly decisions evaluated: {self._rebalances}")
        self.debug("RESEARCH ONLY; compare net returns with SPY and a risk-matched account")
