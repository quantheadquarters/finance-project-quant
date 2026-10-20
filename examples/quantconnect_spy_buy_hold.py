"""Same-window SPY buy-and-hold baseline for the cloud momentum experiment.

Paste into a separate QuantConnect Cloud project's main.py. Research only.
The organization's out-of-sample holdout must remain enabled for both runs.
"""

# ruff: noqa: F403, F405 -- QuantConnect requires its generated star-import module.

from datetime import date

from AlgorithmImports import *


class TenBpsFeeModel(FeeModel):
    def get_order_fee(self, parameters):
        amount = abs(parameters.order.quantity) * parameters.security.price * 0.001
        return OrderFee(CashAmount(amount, "USD"))


class SPYBuyAndHold(QCAlgorithm):
    def initialize(self):
        self.set_start_date(2015, 1, 2)
        self.set_end_date(2026, 9, 21)
        self.set_cash(100_000)
        self._spy = self.add_equity(
            "SPY", Resolution.DAILY, data_normalization_mode=DataNormalizationMode.RAW
        ).symbol
        self.securities[self._spy].set_fee_model(TenBpsFeeModel())
        self.set_benchmark(self._spy)
        self.schedule.on(
            self.date_rules.every_day(self._spy),
            self.time_rules.before_market_open(self._spy, 10),
            self._buy_at_first_candidate_open,
        )
        self.debug("RESEARCH ONLY: SPY buy-and-hold comparator; 10 bps per side")

    def _buy_at_first_candidate_open(self):
        if self.time.date() == date(2015, 1, 5):
            quantity = self.calculate_order_quantity(self._spy, 1.0)
            self.market_on_open_order(self._spy, quantity)

    def on_order_event(self, event):
        if event.status in (OrderStatus.INVALID, OrderStatus.CANCELED):
            self.error(f"ORDER {event.status}: {event.message}")

    def on_end_of_algorithm(self):
        self.debug(
            f"SPY baseline ended on {self.time.date()} "
            f"with equity={self.portfolio.total_portfolio_value}"
        )
