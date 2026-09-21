"""Normalized data shapes. Every external source, however messy, is mapped into
these before anything downstream sees it. An NSE quote and a FRED series land in
compatible shapes here, which is what lets analyzers stay source-agnostic.

If you add a source, you write an ingestion adapter that outputs these types. You
do NOT teach the analyzer about the source's native format. That separation is the
whole point of the cache layer.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field


class Interval(str, Enum):
    MINUTE = "1m"
    HOUR = "1h"
    DAY = "1d"


class Candle(BaseModel):
    """One OHLCV bar, normalized. Volume optional because some macro/forex
    sources don't provide it."""

    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float | None = None


class PriceSeries(BaseModel):
    """A run of candles for one asset at one interval. The bread-and-butter input
    for a technical/trend analyzer."""

    asset: str
    interval: Interval
    candles: list[Candle] = Field(default_factory=list)
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def closes(self) -> list[float]:
        return [c.close for c in self.candles]


class OptionRight(str, Enum):
    """Whether an option is a call (right to buy) or a put (right to sell)."""

    CALL = "call"
    PUT = "put"


class OptionQuote(BaseModel):
    """One row of an options chain: a single strike + right for one expiry.
    `oi` (open interest) is the number of contracts currently open there — the
    footprint of where market participants have positioned. `oi_change` is the
    day-over-day shift when the source provides it; None means unknown, which
    analyzers must treat differently from zero."""

    strike: float
    right: OptionRight
    oi: float = Field(..., ge=0.0, description="open interest, in contracts")
    oi_change: float | None = None
    volume: float | None = None
    last_price: float | None = None


class OptionsChain(BaseModel):
    """A full options chain for one underlying and one expiry, normalized. This
    is the input for the F&O analyzers (PCR, max pain, OI structure). Whatever
    broker it came from (Breeze, Angel One, a hand-dropped JSON), it lands in
    this one shape."""

    underlying: str  # e.g. 'NIFTY'
    expiry: datetime
    spot: float | None = Field(None, description="underlying price when fetched")
    quotes: list[OptionQuote] = Field(default_factory=list)
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def calls(self) -> list[OptionQuote]:
        return [q for q in self.quotes if q.right is OptionRight.CALL]

    def puts(self) -> list[OptionQuote]:
        return [q for q in self.quotes if q.right is OptionRight.PUT]


class MacroObservation(BaseModel):
    """A single value of a macro series at a date, e.g. US CPI for a month.
    Normalized from FRED, World Bank, RBI, etc."""

    series_id: str  # e.g. 'CPIAUCSL'
    ts: datetime
    value: float
    source: str  # e.g. 'fred'


class NewsItem(BaseModel):
    """One headline, normalized from any feed (RSS, EDGAR, a news API).

    `sentiment_score` is None until an analyzer scores it. It is deliberately
    part of the *data* shape rather than computed on every read, because
    scoring is deterministic and caching it per-headline is what keeps the
    analyze path free of repeated work.

    `asset_tags` is how a headline reaches an asset. An untagged item is still
    stored — it may be macro-relevant — but no asset-level analyzer will see it.
    """

    ts: datetime
    headline: str
    source: str  # e.g. 'sec_edgar', 'nse_announcements'
    url: str = ""
    asset_tags: list[str] = Field(default_factory=list)
    sentiment_score: float | None = Field(
        None, ge=-1.0, le=1.0, description="-1 maximally negative, +1 maximally positive"
    )


class OnChainObservation(BaseModel):
    """One crypto market-structure or on-chain metric reading.

    Covers both true on-chain data (exchange flows, active addresses) and
    derivatives positioning (funding rate, open interest), because from the
    analyzer's point of view they answer the same question: what is the
    positioning underneath the price?
    """

    metric: str  # e.g. 'funding_rate', 'open_interest', 'btc_dominance'
    ts: datetime
    value: float
    chain: str = ""  # e.g. 'bitcoin'; empty for exchange-derived metrics
    source: str = ""  # e.g. 'binance_futures', 'glassnode'


class Fundamentals(BaseModel):
    """One reporting period of company fundamentals, normalized.

    Every field is optional because no free source provides all of them for all
    companies, and a missing margin must stay missing rather than becoming
    zero — a zero margin and an unknown margin lead to opposite conclusions.
    """

    asset: str
    period: str  # e.g. '2024-Q3' or '2024'
    ts: datetime  # reporting-period end
    available_at: datetime | None = None  # when the filing became public; required for backtests
    revenue: float | None = None
    net_income: float | None = None
    operating_cash_flow: float | None = None
    gross_margin: float | None = None
    total_debt: float | None = None
    total_equity: float | None = None
    shares_outstanding: float | None = None
    source: str = ""


class EventItem(BaseModel):
    """A scheduled, known-in-advance event: an RBI MPC date, an FOMC decision,
    a CPI print, an earnings date.

    Its use in this engine is defensive, not predictive. A signal fired the day
    before a policy decision deserves lower confidence, and that adjustment must
    be deterministic — which requires knowing the calendar as data.
    """

    ts: datetime
    name: str  # e.g. 'FOMC rate decision'
    region: str  # 'us', 'in', 'global'
    importance: str = "medium"  # 'high' | 'medium' | 'low'
    asset_tags: list[str] = Field(default_factory=list)
    source: str = ""
