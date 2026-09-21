"""Tests for Phase 12: the event-driven orchestrator.

Two properties matter more than the rest:

1. **Ordering is deterministic.** An orchestrator that reorders work
   non-deterministically makes every run unreproducible, which breaks the
   guarantee the whole codebase is built on.
2. **A news event triggers a targeted re-scan, not a portfolio sweep.** That is
   the literal "done when" condition Phase 12 declares.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from alpha_engine.cache.interface import Cache, LocalStore
from alpha_engine.cache.models import Candle, Interval, NewsItem, PriceSeries
from alpha_engine.orchestrator.engine import (
    Priority,
    RefreshReport,
    SharedContext,
    Trigger,
    TriggerKind,
    TriggerQueue,
    is_data_stale,
    refresh_context,
    scheduled_trigger,
    stale_kinds,
    triggers_from_news,
    user_trigger,
)

NOW = datetime.now(timezone.utc)


def _cache(tmp_path) -> Cache:
    return Cache(LocalStore(tmp_path))


def _series(asset: str = "BTC", n: int = 30) -> PriceSeries:
    return PriceSeries(
        asset=asset,
        interval=Interval.DAY,
        candles=[
            Candle(ts=NOW - timedelta(days=n - i), open=100, high=101, low=99, close=100)
            for i in range(n)
        ],
    )


def _news(headline: str, tags: list[str], hours_ago: float = 1.0) -> NewsItem:
    return NewsItem(
        ts=NOW - timedelta(hours=hours_ago),
        headline=headline,
        source="test",
        asset_tags=tags,
    )


# ---------------------------------------------------------------------------
# Priority queue
# ---------------------------------------------------------------------------


def test_empty_queue_pops_none():
    assert TriggerQueue().pop() is None


def test_higher_priority_runs_first():
    q = TriggerQueue()
    q.push(scheduled_trigger(("BTC",)))
    q.push(user_trigger(("ETH",)))
    assert q.pop().kind == TriggerKind.USER_QUERY


def test_news_preempts_the_routine_sweep():
    """A breaking headline should not wait behind forty unrelated tickers."""
    q = TriggerQueue()
    q.push(scheduled_trigger(("BTC", "ETH", "AAPL")))
    q.push(Trigger(kind=TriggerKind.NEWS_EVENT, assets=("NVDA",), priority=Priority.EVENT))
    assert q.pop().kind == TriggerKind.NEWS_EVENT


def test_equal_priority_preserves_insertion_order():
    """Ties must break deterministically, or runs stop being reproducible."""
    q = TriggerQueue()
    for asset in ("A", "B", "C", "D"):
        q.push(Trigger(kind=TriggerKind.SCHEDULED, assets=(asset,), priority=Priority.ROUTINE))
    assert [t.assets[0] for t in q.drain()] == ["A", "B", "C", "D"]


def test_drain_returns_full_priority_order():
    q = TriggerQueue()
    q.push(scheduled_trigger(("routine",)))
    q.push(Trigger(kind=TriggerKind.NEWS_EVENT, assets=("event",), priority=Priority.EVENT))
    q.push(user_trigger(("user",)))
    assert [t.assets[0] for t in q.drain()] == ["user", "event", "routine"]


def test_drain_empties_the_queue():
    q = TriggerQueue()
    q.push(scheduled_trigger(("BTC",)))
    q.drain()
    assert len(q) == 0 and not q


def test_queue_truthiness():
    q = TriggerQueue()
    assert not q
    q.push(scheduled_trigger(("BTC",)))
    assert q


def test_ordering_is_reproducible_across_runs():
    def build() -> list[str]:
        q = TriggerQueue()
        q.push(scheduled_trigger(("X",)))
        q.push(Trigger(kind=TriggerKind.NEWS_EVENT, assets=("Y",), priority=Priority.EVENT))
        q.push(Trigger(kind=TriggerKind.NEWS_EVENT, assets=("Z",), priority=Priority.EVENT))
        return [t.assets[0] for t in q.drain()]

    assert build() == build()


# ---------------------------------------------------------------------------
# Trigger -> analyzer mapping
# ---------------------------------------------------------------------------


def test_news_trigger_wakes_a_narrow_set():
    """A headline needs the asset re-read, not the whole factor panel."""
    analyzers = Trigger(kind=TriggerKind.NEWS_EVENT, assets=("BTC",)).analyzers()
    assert "news" in analyzers
    assert "fundamentals" not in analyzers


def test_scheduled_trigger_wakes_everything():
    assert set(scheduled_trigger(("BTC",)).analyzers()) >= {"price", "macro", "news"}


def test_unknown_trigger_kind_falls_back_to_price():
    assert Trigger(kind="invented", assets=("BTC",)).analyzers() == ("price",)


# ---------------------------------------------------------------------------
# Shared context — one fetch per series per run
# ---------------------------------------------------------------------------


def test_series_is_fetched_once_and_reused(tmp_path):
    """The Phase 12 'done when': a single run fetches each series once."""
    cache = _cache(tmp_path)
    cache.put_price(_series("BTC"))
    ctx = SharedContext(cache)

    for _ in range(5):
        ctx.get_series("BTC")

    assert ctx.fetch_count == 1
    assert ctx.hit_count == 4


def test_different_assets_fetch_separately(tmp_path):
    cache = _cache(tmp_path)
    cache.put_price(_series("BTC"))
    cache.put_price(_series("ETH"))
    ctx = SharedContext(cache)
    ctx.get_series("BTC")
    ctx.get_series("ETH")
    assert ctx.fetch_count == 2


def test_asset_lookup_is_case_insensitive(tmp_path):
    cache = _cache(tmp_path)
    cache.put_price(_series("BTC"))
    ctx = SharedContext(cache)
    ctx.get_series("btc")
    ctx.get_series("BTC")
    assert ctx.fetch_count == 1


def test_missing_series_is_cached_as_missing(tmp_path):
    """A miss must be memoized too, or every analyzer retries the same absent
    series."""
    ctx = SharedContext(_cache(tmp_path))
    assert ctx.get_series("NOPE") is None
    assert ctx.get_series("NOPE") is None
    assert ctx.fetch_count == 1


def test_put_series_is_visible_to_later_reads(tmp_path):
    ctx = SharedContext(_cache(tmp_path))
    ctx.put_series(_series("SOL"))
    assert ctx.get_series("SOL") is not None
    assert ctx.fetch_count == 0  # served from the memo, never read


def test_news_is_loaded_once(tmp_path):
    cache = _cache(tmp_path)
    cache.put_news("test", [_news("Profit surges", ["BTC"])])
    ctx = SharedContext(cache)
    assert ctx.get_news() is ctx.get_news()


def test_context_stats_report_reuse(tmp_path):
    cache = _cache(tmp_path)
    cache.put_price(_series("BTC"))
    ctx = SharedContext(cache)
    ctx.get_series("BTC")
    ctx.get_series("BTC")
    stats = ctx.stats()
    assert stats["series_loaded"] == 1 and stats["series_reused"] == 1


# ---------------------------------------------------------------------------
# News-driven targeted triggers
# ---------------------------------------------------------------------------


def test_news_produces_a_targeted_trigger(tmp_path):
    cache = _cache(tmp_path)
    cache.put_news("test", [_news("Apple profit surges", ["AAPL"])])
    triggers = triggers_from_news(cache, ("AAPL", "MSFT", "BTC"))
    assert len(triggers) == 1
    assert triggers[0].assets == ("AAPL",)
    assert triggers[0].priority == Priority.EVENT


def test_news_does_not_trigger_a_full_sweep(tmp_path):
    """The literal Phase 12 requirement: one company's headline must not
    re-scan the other thirty-nine tickers."""
    cache = _cache(tmp_path)
    cache.put_news("test", [_news("Apple profit surges", ["AAPL"])])
    portfolio = ("AAPL", "MSFT", "GOOGL", "NVDA", "BTC", "ETH")
    affected = {a for t in triggers_from_news(cache, portfolio) for a in t.assets}
    assert affected == {"AAPL"}


def test_untracked_asset_tags_are_ignored(tmp_path):
    cache = _cache(tmp_path)
    cache.put_news("test", [_news("Tesla profit surges", ["TSLA"])])
    assert triggers_from_news(cache, ("AAPL", "BTC")) == []


def test_stale_headlines_do_not_trigger(tmp_path):
    cache = _cache(tmp_path)
    cache.put_news("test", [_news("Old news", ["AAPL"], hours_ago=48)])
    assert triggers_from_news(cache, ("AAPL",), max_age_hours=6) == []


def test_repeated_headlines_about_one_asset_produce_one_trigger(tmp_path):
    """Two stories about the same company is still one thing to re-read."""
    cache = _cache(tmp_path)
    cache.put_news(
        "test",
        [_news("Apple beats", ["AAPL"]), _news("Apple upgraded", ["AAPL"])],
    )
    assert len(triggers_from_news(cache, ("AAPL",))) == 1


def test_no_news_means_no_triggers(tmp_path):
    assert triggers_from_news(_cache(tmp_path), ("AAPL",)) == []


def test_news_triggers_are_deterministically_ordered(tmp_path):
    cache = _cache(tmp_path)
    cache.put_news(
        "test",
        [_news("Z news", ["NVDA"]), _news("A news", ["AAPL"]), _news("M news", ["MSFT"])],
    )
    portfolio = ("AAPL", "MSFT", "NVDA")
    first = [t.assets[0] for t in triggers_from_news(cache, portfolio)]
    second = [t.assets[0] for t in triggers_from_news(cache, portfolio)]
    assert first == second == ["AAPL", "MSFT", "NVDA"]


# ---------------------------------------------------------------------------
# Freshness-driven ingestion
# ---------------------------------------------------------------------------


def test_empty_cache_is_stale_in_every_kind(tmp_path):
    stale = stale_kinds(_cache(tmp_path), ("BTC",))
    assert {"news", "onchain", "price"} <= stale
    assert "fundamentals" not in stale  # BTC has no company filings


def test_empty_apple_fundamentals_need_refresh_when_sec_is_configured(monkeypatch, tmp_path):
    from alpha_engine.ingestion import fmp, sec_fundamentals

    monkeypatch.setattr(fmp, "has_key", lambda: False)
    monkeypatch.setattr(sec_fundamentals, "has_user_agent", lambda: True)
    assert "fundamentals" in stale_kinds(_cache(tmp_path), ("AAPL",))


def test_fresh_news_is_not_reported_stale(tmp_path):
    cache = _cache(tmp_path)
    cache.put_news("test", [_news("Fresh headline", ["BTC"], hours_ago=0)])
    assert "news" not in stale_kinds(cache, ("BTC",))


def test_fresh_price_is_not_reported_stale(tmp_path):
    cache = _cache(tmp_path)
    cache.put_price(_series("BTC"))
    assert "price" not in stale_kinds(cache, ("BTC",))


def test_is_data_stale_on_missing_series(tmp_path):
    assert is_data_stale(_cache(tmp_path), "NOPE") is True


def test_is_data_stale_on_fresh_series(tmp_path):
    cache = _cache(tmp_path)
    cache.put_price(_series("BTC"))
    assert is_data_stale(cache, "BTC") is False


def test_refresh_skips_kinds_that_are_fresh(tmp_path, monkeypatch):
    """The whole point of freshness-driven ingestion: do not refetch what is
    already current."""
    cache = _cache(tmp_path)
    called = []

    from alpha_engine.ingestion import rss

    monkeypatch.setattr(rss, "fetch_all", lambda **kw: called.append("rss") or [])

    report = refresh_context(cache, ("BTC",), kinds=set())
    assert called == []
    assert "news" in report.skipped_fresh


def test_refresh_isolates_a_failing_source(tmp_path, monkeypatch):
    """A dead RSS feed must not stop the on-chain refresh."""
    cache = _cache(tmp_path)

    from alpha_engine.ingestion import binance_futures, coingecko, rss

    def boom(**kw):
        raise OSError("feed down")

    monkeypatch.setattr(rss, "fetch_all", boom)
    monkeypatch.setattr(binance_futures, "fetch_all", lambda *a, **kw: [])
    monkeypatch.setattr(coingecko, "fetch_btc_dominance", lambda **kw: None)

    report = refresh_context(cache, ("BTC",), kinds={"news", "onchain"})
    assert "news" in report.failed
    assert "onchain" in report.refreshed


def test_refresh_report_summary_is_serializable():
    report = RefreshReport(refreshed=["news"], skipped_fresh=["onchain"], failed={"x": "boom"})
    summary = report.summary()
    assert summary["refreshed"] == ["news"]
    assert summary["failed"] == {"x": "boom"}
