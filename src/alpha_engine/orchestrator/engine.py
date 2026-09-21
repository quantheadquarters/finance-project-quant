"""The real orchestrator: triggers, priority, shared context, freshness.

`orchestrator/__init__.py` is a batch runner — a for-loop over a portfolio with
error handling. That was the honest tool for one cron job and eleven analyzers.
This module is what FUTURE_WORK's Phase 12 asked for once there were enough data
domains for scheduling to be a real problem.

Four things it adds, and why each one earns its place:

**Triggers.** Work arrives for different reasons. A 9am sweep, a news headline
about one company, a user typing a symbol — these are not the same event and
should not queue the same way. A `Trigger` names why work exists and which
assets it touches.

**Priority.** A breaking earnings headline should preempt the routine morning
scan of forty unrelated tickers. Priority is an integer, lower runs first, and
ties break by arrival time so the ordering is fully deterministic — an
orchestrator that reorders work non-deterministically would make runs
unreproducible, which is the one thing this codebase will not tolerate.

**Shared context.** One fetch of BTC's series serves the trend, RSI, MACD,
volatility and factor consumers in a run, instead of each re-reading the cache.
Today that saves file reads; the moment sources are remote it saves the run.

**Freshness-driven ingestion.** Rather than refetching a fixed list, the
orchestrator asks the cache what has gone stale and refreshes only that. This is
also where the Phase 11 context sources (news, on-chain, fundamentals) get
populated — the scan path reads them cache-only by design, so *something* has to
fill the cache, and this is that something.

Determinism is preserved exactly as before: the orchestrator decides WHAT runs
and WHEN, never HOW anything is analyzed.
"""

from __future__ import annotations

import heapq
import itertools
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import IntEnum
from typing import Any

from alpha_engine import health
from alpha_engine.cache.interface import Cache, is_stale
from alpha_engine.cache.models import PriceSeries
from alpha_engine.ingestion import (
    binance_futures,
    bybit_futures,
    coingecko,
    fmp,
    gate_futures,
    sec_fundamentals,
)
from alpha_engine.schema.signal import Market

#: Futures positioning adapters, tried in order until one returns data.
#
# Every public futures API is blocked from *somewhere*, and the blocks point in
# opposite directions, so no single source works everywhere. Measured
# 2026-07-26 from a US home connection and from a GitHub Actions runner:
#
#     exchange   home     CI
#     binance    200      451   regulatory geo-block on datacenter ranges
#     bybit      200      403   CloudFront country block
#     okx        timeout  200   unreachable from the US residential IP
#     kraken     timeout  200   same
#     gate       200      200
#
# Order is deepest-market-first, then the one that answers in both places.
# Binance carries the most liquidity so it wins where it is reachable; Gate is
# the tier that keeps the scheduled scan working. Re-probe with
# `.github/workflows/probe-exchanges.yml` before reordering this — the answer is
# a property of the network, not of the code, and it changes without notice.
#
# Each entry needs `supports(asset)`, `fetch_all(asset, cache)` and `SOURCE`.
FUTURES_CHAIN = (binance_futures, gate_futures, bybit_futures)


class TriggerKind(str):
    """Why a piece of work exists. A plain str subclass so it serializes into
    reports without special handling."""

    SCHEDULED = "scheduled"
    NEW_DATA = "new_data"
    NEWS_EVENT = "news_event"
    USER_QUERY = "user_query"


class Priority(IntEnum):
    """Lower runs first.

    A user waiting at a terminal beats a headline, which beats the routine
    sweep. These are the only three tiers that have ever mattered; more would be
    a taxonomy rather than a scheduler.
    """

    USER = 0
    EVENT = 10
    ROUTINE = 50


# Which trigger kinds wake which analyzers. The orchestrator uses this to run a
# *targeted* re-scan rather than a full sweep: a news event does not need the
# factor panel recomputed, it needs the affected asset re-read.
TRIGGER_ANALYZERS: dict[str, tuple[str, ...]] = {
    TriggerKind.SCHEDULED: ("price", "macro", "news", "onchain", "fundamentals"),
    TriggerKind.NEW_DATA: ("price",),
    TriggerKind.NEWS_EVENT: ("news", "price"),
    TriggerKind.USER_QUERY: ("price", "macro", "news", "onchain", "fundamentals"),
}


@dataclass(frozen=True, slots=True)
class Trigger:
    """One unit of work: why it exists, what it touches, how urgent it is."""

    kind: str
    assets: tuple[str, ...]
    priority: int = Priority.ROUTINE
    reason: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def analyzers(self) -> tuple[str, ...]:
        return TRIGGER_ANALYZERS.get(self.kind, ("price",))


class TriggerQueue:
    """A deterministic priority queue.

    Ties break by insertion order via a monotonic counter, never by object
    identity or hash. Two runs given the same triggers must execute them in the
    same order, or nothing downstream is reproducible.
    """

    def __init__(self) -> None:
        self._heap: list[tuple[int, int, Trigger]] = []
        self._counter = itertools.count()

    def push(self, trigger: Trigger) -> None:
        heapq.heappush(self._heap, (trigger.priority, next(self._counter), trigger))

    def pop(self) -> Trigger | None:
        return heapq.heappop(self._heap)[2] if self._heap else None

    def drain(self) -> list[Trigger]:
        """Every trigger, in execution order, emptying the queue."""
        out = []
        while (t := self.pop()) is not None:
            out.append(t)
        return out

    def __len__(self) -> int:
        return len(self._heap)

    def __bool__(self) -> bool:
        return bool(self._heap)


class SharedContext:
    """Per-run memo of fetched data, so one run fetches each series once.

    Deliberately a per-run object rather than a process-level cache: a long-lived
    memo would serve yesterday's prices to today's scan, which is precisely the
    bug the TTL system exists to prevent. It lives for one run and dies.
    """

    def __init__(self, cache: Cache | None = None) -> None:
        self.cache = cache or Cache()
        self._series: dict[tuple[str, str], PriceSeries | None] = {}
        self._macro: dict[str, list[Any]] | None = None
        self._news: list[Any] | None = None
        self.fetch_count = 0
        self.hit_count = 0

    def get_series(self, asset: str, interval: str = "1d") -> PriceSeries | None:
        key = (asset.upper(), interval)
        if key in self._series:
            self.hit_count += 1
            return self._series[key]
        series, _stale = self.cache.get_price(asset, interval)
        self._series[key] = series
        self.fetch_count += 1
        return series

    def put_series(self, series: PriceSeries) -> None:
        """Record a freshly fetched series so the rest of the run reuses it."""
        self._series[(series.asset.upper(), series.interval.value)] = series

    def get_news(self) -> list[Any]:
        if self._news is None:
            self._news, _ = self.cache.get_news()
        return self._news

    def stats(self) -> dict[str, int]:
        return {
            "series_loaded": self.fetch_count,
            "series_reused": self.hit_count,
            "distinct_series": len(self._series),
        }


# ---------------------------------------------------------------------------
# Freshness-driven ingestion
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class RefreshReport:
    """What a freshness pass actually did. Reported rather than logged-and-lost,
    because "the news analyzer said nothing" and "the news feed never refreshed"
    look identical from the outside and have completely different fixes."""

    refreshed: list[str] = field(default_factory=list)
    skipped_fresh: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    # How many items each source actually returned. "refreshed: news" hides a
    # silently broken feed; "news: 0 items" reveals it.
    item_counts: dict[str, int] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        return {
            "refreshed": sorted(self.refreshed),
            "skipped_fresh": sorted(self.skipped_fresh),
            "failed": self.failed,
            "item_counts": self.item_counts,
        }


def stale_kinds(cache: Cache, assets: tuple[str, ...]) -> set[str]:
    """Which data kinds have gone stale and are worth refetching.

    This is the "ask what is stale" half of freshness-driven ingestion. It reads
    TTLs from the cache layer rather than duplicating a schedule here, so the
    TTL table stays the single source of truth about how fast each kind rots.
    """
    stale: set[str] = set()

    news, news_stale = cache.get_news()
    if not news or news_stale:
        stale.add("news")

    onchain, onchain_stale = cache.get_onchain()
    if not onchain or onchain_stale:
        stale.add("onchain")

    for asset in assets:
        # Crypto cannot have company filings. Do not keep the whole context
        # pipeline stale forever because BTC has no balance sheet.
        if coingecko.supports(asset) or (
            not fmp.has_key()
            and (not sec_fundamentals.supports(asset) or not sec_fundamentals.has_user_agent())
        ):
            continue
        rows, fund_stale = cache.get_fundamentals(asset)
        if not rows or fund_stale:
            stale.add("fundamentals")
            break

    for asset in assets:
        series, price_stale = cache.get_price(asset, "1d")
        if series is None or price_stale:
            stale.add("price")
            break

    events, events_stale = cache.get_events()
    if not events or events_stale:
        stale.add("events")

    return stale


def refresh_context(
    cache: Cache,
    assets: tuple[str, ...],
    kinds: set[str] | None = None,
    force: bool = False,
) -> RefreshReport:
    """Refresh the Phase 11 context sources that have gone stale.

    Every source is optional and every failure is isolated: a dead RSS feed must
    not stop the on-chain refresh, and neither must stop the scan that follows.
    """
    from alpha_engine.analyzers.sentiment import score_news
    from alpha_engine.ingestion import (
        calendar_file,
        coingecko,
        finnhub_news,
        fmp,
        fomc_calendar,
        glassnode,
        rss,
    )

    report = RefreshReport()
    wanted = kinds if kinds is not None else stale_kinds(cache, assets)
    if force:
        wanted = {"news", "onchain", "fundamentals", "events"}

    def run(kind: str, fetch: Callable[[], int | dict[str, int]], enabled: bool = True) -> None:
        """Run one source's refresh, isolate its failure, and record its health.

        The health record is the part that matters over months. Every adapter
        here degrades to empty rather than raising, so without this a source
        that broke in March looks identical to a quiet Tuesday until someone
        notices the signals got worse. `items` is what separates them.

        A fetcher may return a plain count, or a `{feed: count}` map when the
        kind has several independent feeds. **Return the map whenever it can.**
        An aggregate hides a dead feed behind a live one, and that is not a
        hypothetical: `onchain` recorded a single total, so when Binance began
        answering HTTP 451 to every request from CI, CoinGecko's one dominance
        reading kept the total at "1 item, ok" and six dead fetches stayed
        invisible for a month of green builds.

        Only feeds that were actually *attempted* appear in the map. A feed that
        was never tried must not be recorded, or an unused fallback would age
        into a false "degraded" alarm and teach you to ignore the health table.
        """
        if kind not in wanted or not enabled:
            report.skipped_fresh.append(kind)
            return
        try:
            result = fetch()
        except Exception as e:  # noqa: BLE001 - one dead source is not a failed run
            report.failed[kind] = str(e)
            health.record(kind, error=f"{type(e).__name__}: {e}")
            return

        report.refreshed.append(kind)
        if isinstance(result, dict):
            for feed, count in sorted(result.items()):
                report.item_counts[f"{kind}.{feed}"] = count
                health.record(f"{kind}.{feed}", items=count)
            total = sum(result.values())
        else:
            total = result
        report.item_counts[kind] = total
        health.record(kind, items=total)

    def _news() -> int:
        fetched = rss.fetch_all(cache=cache)
        for asset in assets:
            if finnhub_news.has_key():
                fetched.extend(finnhub_news.fetch_company_news(asset, cache=cache))
        # Score once here rather than on every scan. Sentiment is deterministic,
        # so a cached score is the same score forever — and this is what keeps
        # the analyze path free of repeated work.
        by_source: dict[str, list] = {}
        for item in score_news(fetched):
            by_source.setdefault(item.source, []).append(item)
        for source, items in by_source.items():
            cache.put_news(source, items)
        return len(fetched)

    def _events() -> int:
        # The macro calendar. Without this the event cache stays empty,
        # calendar_scalar() is permanently 1.0, and the whole defensive layer is
        # dead code that looks alive.
        #
        # Two sources, merged: FOMC dates are scraped from federalreserve.gov
        # (that page is structured and parses reliably), and everything else —
        # RBI MPC, CPI prints, earnings — comes from the user's calendar.json.
        # The RBI's own MPC page serves no dates in its HTML.
        fomc = fomc_calendar.fetch_fomc_calendar(cache=cache)
        manual = calendar_file.load_calendar(cache=cache)
        return len(fomc) + len(manual)

    def _onchain() -> dict[str, int]:
        """Per-feed counts, because the aggregate is what hid the CI outage.

        Futures positioning comes from the first adapter in FUTURES_CHAIN that
        actually answers. A chain rather than a single fallback because the
        blocks point in opposite directions — measured 2026-07-26, Binance and
        Bybit answer from a home connection but not from a GitHub runner, while
        OKX and Kraken answer from the runner but not from the home connection.
        Any single second choice fixes one environment and breaks the other.
        Gate.io answers in both, which is why it sits where it does.

        The choice *latches*. A geo-block is a property of the host, not of the
        symbol: once an adapter has returned nothing for one asset it will for
        all of them, so re-probing per asset spends a doomed request per asset
        per run to relearn the same fact.
        """
        counts: dict[str, int] = {}
        chain = list(FUTURES_CHAIN)
        futures_source = None  # latched once one adapter answers

        for asset in assets:
            if glassnode.has_key():
                counts["glassnode"] = counts.get("glassnode", 0) + len(
                    glassnode.fetch_all(asset, cache=cache)
                )

            if futures_source is not None:
                if futures_source.supports(asset):
                    counts[futures_source.SOURCE] = counts.get(futures_source.SOURCE, 0) + len(
                        futures_source.fetch_all(asset, cache=cache)
                    )
                continue

            # Nothing chosen yet: walk the chain until something returns data.
            for adapter in chain:
                if not adapter.supports(asset):
                    continue
                fetched = len(adapter.fetch_all(asset, cache=cache))
                counts[adapter.SOURCE] = counts.get(adapter.SOURCE, 0) + fetched
                if fetched:
                    futures_source = adapter
                    break

        counts["coingecko_dominance"] = (
            1 if coingecko.fetch_btc_dominance(cache=cache) is not None else 0
        )
        return counts

    def _fundamentals() -> dict[str, int]:
        counts: dict[str, int] = {}
        for asset in assets:
            if coingecko.supports(asset):
                continue
            if fmp.has_key():
                fetched = fmp.fetch_fundamentals(asset, cache=cache)
                counts["fmp"] = counts.get("fmp", 0) + len(fetched)
            else:
                fetched = []
            if (
                not fetched
                and sec_fundamentals.supports(asset)
                and sec_fundamentals.has_user_agent()
            ):
                counts[f"sec_{asset.lower()}"] = len(
                    sec_fundamentals.fetch_fundamentals(asset, cache=cache)
                )
        return counts

    run("news", _news)
    run("events", _events)
    run("onchain", _onchain)
    run(
        "fundamentals",
        _fundamentals,
        enabled=fmp.has_key()
        or (sec_fundamentals.has_user_agent() and any(map(sec_fundamentals.supports, assets))),
    )

    return report


# ---------------------------------------------------------------------------
# Event-driven triggering
# ---------------------------------------------------------------------------


def triggers_from_news(
    cache: Cache,
    known_assets: tuple[str, ...],
    since: datetime | None = None,
    max_age_hours: float = 6.0,
) -> list[Trigger]:
    """Build targeted re-scan triggers from recent tagged headlines.

    This is the capability Phase 12 is "done when" it has: a news event triggers
    a re-scan of the *affected asset*, not a full portfolio sweep. A headline
    about one company is not a reason to recompute forty unrelated tickers.

    Only headlines tagged with an asset we actually follow produce a trigger —
    an untagged macro headline is context, not a work item.
    """
    now = datetime.now(timezone.utc)
    cutoff = since or now.replace(microsecond=0)

    items, _ = cache.get_news()
    followed = {a.upper() for a in known_assets}

    affected: dict[str, str] = {}
    for item in items:
        age_hours = (now - item.ts).total_seconds() / 3600.0
        if age_hours > max_age_hours or age_hours < 0:
            continue
        for tag in item.asset_tags:
            if tag.upper() in followed and tag.upper() not in affected:
                affected[tag.upper()] = item.headline

    return [
        Trigger(
            kind=TriggerKind.NEWS_EVENT,
            assets=(asset,),
            priority=Priority.EVENT,
            reason=f"news: {headline[:80]}",
            created_at=cutoff,
        )
        for asset, headline in sorted(affected.items())
    ]


def scheduled_trigger(assets: tuple[str, ...]) -> Trigger:
    """The routine sweep. Lowest priority: it is the work that can always wait."""
    return Trigger(
        kind=TriggerKind.SCHEDULED,
        assets=assets,
        priority=Priority.ROUTINE,
        reason="scheduled sweep",
    )


def user_trigger(assets: tuple[str, ...]) -> Trigger:
    """Someone is waiting at a terminal. Highest priority, always."""
    return Trigger(
        kind=TriggerKind.USER_QUERY,
        assets=assets,
        priority=Priority.USER,
        reason="user request",
    )


# ---------------------------------------------------------------------------
# The run loop
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class OrchestrationReport:
    """Outcome of one orchestrated run."""

    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None
    executed: list[dict[str, Any]] = field(default_factory=list)
    refresh: RefreshReport | None = None
    context_stats: dict[str, int] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "triggers_executed": len(self.executed),
            "executed": self.executed,
            "refresh": self.refresh.summary() if self.refresh else None,
            "context": self.context_stats,
        }


def run_triggers(
    queue: TriggerQueue,
    config: Any,
    cache: Cache | None = None,
    refresh: bool = True,
) -> OrchestrationReport:
    """Execute every queued trigger in priority order, sharing one context.

    `config` is an `OrchestratorConfig` from `orchestrator/__init__.py`; it is
    typed loosely here purely to avoid a circular import, since that module
    imports this one's siblings.
    """
    from alpha_engine.orchestrator import AssetTarget, scan_target

    cache = cache or Cache()
    ctx = SharedContext(cache)
    report = OrchestrationReport()

    ordered = queue.drain()
    all_assets = tuple(sorted({a for t in ordered for a in t.assets}))

    if refresh and all_assets:
        report.refresh = refresh_context(cache, all_assets)

    # One scan per asset per run, even if several triggers name the same asset:
    # two headlines about one company is still one thing worth re-reading.
    already_scanned: set[str] = set()

    for trigger in ordered:
        for asset in trigger.assets:
            if asset in already_scanned:
                report.executed.append(
                    {
                        "asset": asset,
                        "trigger": trigger.kind,
                        "status": "deduped",
                        "reason": trigger.reason,
                    }
                )
                continue
            already_scanned.add(asset)

            start = time.monotonic()
            target = AssetTarget(asset=asset, market=_market_for(asset))
            result = scan_target(target, cache, config)

            # Feed anything the scan fetched back into the shared context so a
            # later trigger touching the same series does not re-read it.
            series = ctx.get_series(asset)
            if series is not None:
                ctx.put_series(series)

            report.executed.append(
                {
                    "asset": asset,
                    "trigger": trigger.kind,
                    "priority": int(trigger.priority),
                    "status": result.status,
                    "reason": trigger.reason,
                    "direction": result.signal.direction.value if result.signal else None,
                    "confidence": result.signal.confidence if result.signal else None,
                    "error": result.error,
                    "duration_ms": round((time.monotonic() - start) * 1000, 1),
                }
            )
            print(
                f"[orchestrator] {trigger.kind:<12} {asset:<10} -> {result.status}",
                file=sys.stderr,
            )

    report.context_stats = ctx.stats()
    report.finished_at = datetime.now(timezone.utc)
    return report


def _market_for(asset: str) -> Market:
    """Reuse the CLI's detection so the orchestrator and a manual scan never
    disagree about what an asset is."""
    from alpha_engine.cli.main import detect_market

    return detect_market(asset)


def is_data_stale(cache: Cache, asset: str, interval: str = "1d") -> bool:
    """Whether an asset's price series needs refetching. Exposed so a caller can
    build NEW_DATA triggers without reaching into the cache internals."""
    series, stale = cache.get_price(asset, interval)
    if series is None:
        return True
    return stale or is_stale(series.fetched_at, "price", interval)
