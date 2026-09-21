"""Tests for cache retention.

Retention exists to stop the Phase-11 collections growing forever. Measured
before it existed: a conservative 40 headlines/day produced 14,600 news items
and 2.9 MB after one year, of which ~5% were recent enough for any analyzer to
read — and every scan parsed all of it while every write re-serialized all of
it. That is the "works for a week then gets slow and weird" failure.

The risk in fixing it is the opposite failure: pruning something an analyzer
still needs, which would show up as a source quietly saying less than it should.
So the tests below pin retention windows *against the analyzer lookbacks they
serve*, and those pairings are what stop the two drifting apart.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from alpha_engine.analyzers.sentiment import MAX_AGE_DAYS
from alpha_engine.cache.interface import (
    EVENT_PAST_RETENTION,
    RETENTION,
    Cache,
    LocalStore,
    prune_collection,
)
from alpha_engine.cache.models import EventItem, Fundamentals, NewsItem, OnChainObservation

NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)


def _news(days_ago: float, key: str = "x") -> NewsItem:
    return NewsItem(
        ts=NOW - timedelta(days=days_ago),
        headline=f"headline {key}",
        source="test",
        url=f"http://example.com/{key}/{days_ago}",
    )


def _event(days_from_now: float, name: str = "e") -> EventItem:
    return EventItem(ts=NOW + timedelta(days=days_from_now), name=name, region="us")


# ---------------------------------------------------------------------------
# The window must cover what the analyzer reads
# ---------------------------------------------------------------------------


def test_news_retention_outlives_the_sentiment_lookback():
    """The binding constraint. If someone raises MAX_AGE_DAYS past the
    retention window, the analyzer starts asking for data the cache already
    deleted — and silently gets a weaker read instead of an error."""
    assert RETENTION["news"] > timedelta(days=MAX_AGE_DAYS)


def test_fundamentals_retention_covers_year_over_year_growth():
    """revenue_growth() reaches back 5 quarters (~15 months)."""
    assert RETENTION["fundamentals"] > timedelta(days=460)


def test_fundamentals_freshness_uses_fetch_time_not_quarter_end(tmp_path):
    cache = Cache(LocalStore(tmp_path))
    cache.put_fundamentals(
        "AAPL",
        [
            Fundamentals(
                asset="AAPL", period="2025-Q1", ts=datetime.now(timezone.utc) - timedelta(days=90)
            )
        ],
    )
    items, stale = cache.get_fundamentals("AAPL")
    assert len(items) == 1
    assert stale is False


def test_onchain_retention_is_generous_enough_for_history():
    assert RETENTION["onchain"] >= timedelta(days=365)


# ---------------------------------------------------------------------------
# Pruning behaviour
# ---------------------------------------------------------------------------


def test_prune_drops_items_past_the_window():
    items = [_news(1, "fresh"), _news(400, "ancient")]
    kept = prune_collection("news", items, now=NOW)
    assert len(kept) == 1
    assert kept[0].headline == "headline fresh"


def test_prune_keeps_everything_inside_the_window():
    items = [_news(d, str(d)) for d in (0, 1, 5, 20, 29)]
    assert len(prune_collection("news", items, now=NOW)) == 5


def test_prune_keeps_items_exactly_on_the_boundary():
    """An off-by-one here silently deletes a day of data every day."""
    edge = RETENTION["news"].days
    assert len(prune_collection("news", [_news(edge)], now=NOW)) == 1


def test_prune_is_a_noop_for_unknown_kinds():
    """An unrecognized kind must never be silently emptied."""
    items = [_news(9999)]
    assert prune_collection("something_new", items, now=NOW) == items


def test_prune_keeps_undateable_items():
    """Refusing to delete what we cannot date is the safe direction to fail."""

    class NoTs:
        ts = None

    item = NoTs()
    assert prune_collection("news", [item], now=NOW) == [item]


def test_prune_handles_empty_input():
    assert prune_collection("news", [], now=NOW) == []


# ---------------------------------------------------------------------------
# Events prune differently — the future is the point
# ---------------------------------------------------------------------------


def test_future_events_are_never_pruned():
    """A calendar that forgets tomorrow's FOMC is worse than no calendar."""
    far_future = _event(3650, "FOMC 2036")
    assert prune_collection("events", [far_future], now=NOW) == [far_future]


def test_recent_past_events_are_kept():
    assert len(prune_collection("events", [_event(-10)], now=NOW)) == 1


def test_long_past_events_are_dropped():
    stale = _event(-(EVENT_PAST_RETENTION.days + 10))
    assert prune_collection("events", [stale], now=NOW) == []


# ---------------------------------------------------------------------------
# End to end through the real store
# ---------------------------------------------------------------------------


def test_writes_prune_on_the_way_in(tmp_path):
    """Anchored to the real clock, because `write_collection` prunes against
    `datetime.now()` — a fixed fixture date would drift out of the window as
    real time passes and make this test rot."""
    now = datetime.now(timezone.utc)
    cache = Cache(LocalStore(tmp_path))
    cache.put_news(
        "test",
        [
            NewsItem(ts=now - timedelta(days=1), headline="fresh", source="t", url="u-fresh"),
            NewsItem(ts=now - timedelta(days=500), headline="ancient", source="t", url="u-old"),
        ],
    )
    items, _ = cache.get_news()
    assert [i.headline for i in items] == ["fresh"]


def test_repeated_writes_do_not_grow_without_bound(tmp_path):
    """The actual regression test: simulate a year of daily ingests and assert
    the file stays bounded instead of reaching ~15k rows."""
    store = LocalStore(tmp_path)

    now = datetime.now(timezone.utc)
    for day in range(365):
        ts = now - timedelta(days=365 - day)
        batch = [
            NewsItem(
                ts=ts,
                headline=f"day {day} item {i}",
                source="rss",
                url=f"http://example.com/{day}/{i}",
            )
            for i in range(40)
        ]
        # prune relative to that day, mirroring a real daily run
        store.write_collection("news", "rss", batch)

    items = store.read_collection("news", "rss")
    # 30-day window x 40/day = ~1200, versus 14,600 unbounded
    assert len(items) < 2000, f"news grew to {len(items)} rows; retention is not working"


def test_pruning_preserves_what_the_analyzer_actually_reads(tmp_path):
    """The failure mode that matters more than growth: pruning must not weaken
    a read. Sentiment over the kept window must match sentiment over everything."""
    from alpha_engine.analyzers.sentiment import analyze_sentiment

    now = datetime.now(timezone.utc)
    everything = [
        NewsItem(
            ts=now - timedelta(days=d),
            headline="Profit surges to record",
            source="t",
            url=f"u{d}",
            asset_tags=["BTC"],
        )
        for d in (0, 1, 2, 10, 20, 100, 300)
    ]
    kept = prune_collection("news", everything, now=now)

    full = analyze_sentiment(everything, asset="BTC", now=now)
    pruned = analyze_sentiment(kept, asset="BTC", now=now)
    assert (full.direction, full.weight) == (pruned.direction, pruned.weight)


def test_onchain_pruning_preserves_the_analyzer_read(tmp_path):
    from alpha_engine.analyzers.crypto_onchain import analyze_onchain

    now = datetime.now(timezone.utc)
    obs = [
        OnChainObservation(
            metric="funding_rate_BTC", ts=now - timedelta(days=d), value=0.001, source="t"
        )
        for d in (0, 1, 2, 3, 500, 900)
    ]
    kept = prune_collection("onchain", obs, now=now)
    assert analyze_onchain(kept, "BTC").weight == analyze_onchain(obs, "BTC").weight


def test_fundamentals_survive_long_enough_for_growth(tmp_path):
    from alpha_engine.analyzers.fundamentals import revenue_growth

    now = datetime.now(timezone.utc)
    periods = [
        Fundamentals(
            asset="X", period=f"q{i}", ts=now - timedelta(days=90 * (4 - i)), revenue=1000.0 + i
        )
        for i in range(5)
    ]
    kept = prune_collection("fundamentals", periods, now=now)
    assert len(kept) == 5
    assert revenue_growth(kept) == revenue_growth(periods)


def test_concurrent_cache_writes_do_not_crash(tmp_path):
    """Regression: the temp filename keyed only on PID, so two threads collided
    and the losing rename raised FileNotFoundError. Atomic rename keeps the file
    intact; the thread id keeps the writers from fighting over one temp path."""
    import threading

    now = datetime.now(timezone.utc)
    errors: list[str] = []

    def worker(n: int) -> None:
        try:
            cache = Cache(LocalStore(tmp_path))
            for i in range(25):
                cache.put_news(
                    "shared",
                    [NewsItem(ts=now, headline=f"w{n}-{i}", source="shared", url=f"w{n}/{i}")],
                )
        except Exception as e:  # noqa: BLE001 - the point is to catch any crash
            errors.append(f"{type(e).__name__}: {e}")

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"concurrent cache writes raised: {errors[:2]}"
    items, _ = Cache(LocalStore(tmp_path)).get_news("shared")
    assert items  # readable, not corrupt
    assert list((tmp_path / "news").glob("*.tmp")) == []
