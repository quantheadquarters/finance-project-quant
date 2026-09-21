"""The cache interface. This is the seam the plan insists on: analyzers read from
HERE, never from the network. An ingestion service (Phase 1, separate process or
scheduled job) keeps the store fresh; consumers just read.

The default backend is a local JSON store so a freshly cloned repo runs
with zero infrastructure. Swapping in Postgres/Timescale later means writing
a second store class and adjusting the Cache() default; nothing upstream changes.

Freshness: every kind has a TTL. A quote goes stale in seconds, a CPI print in a
month. `get_price`/`get_macro` return data plus whether it's stale, so a consumer
can decide whether to trigger a refresh. The cache never silently serves rot.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from alpha_engine.config import data_dir
from alpha_engine.cache.models import (
    EventItem,
    Fundamentals,
    MacroObservation,
    NewsItem,
    OnChainObservation,
    OptionsChain,
    PriceSeries,
)

# TTL budget per data kind. Tune as you learn each source's update cadence.
TTL: dict[str, timedelta] = {
    "price:1m": timedelta(minutes=2),
    "price:1h": timedelta(hours=1),
    "price:1d": timedelta(hours=12),
    "macro": timedelta(days=1),
    "chain": timedelta(minutes=15),  # OI moves intraday; chains rot fast
    "news": timedelta(hours=2),  # headlines matter fast, but not second-by-second
    "onchain": timedelta(hours=1),  # funding prints 3x/day, OI moves intraday
    "fundamentals": timedelta(days=7),  # quarterly data; a weekly check is generous
    "events": timedelta(days=1),  # a calendar changes rarely and predictably
}


# How long each collection is kept before old rows are dropped on write.
#
# Without this the collections grow forever. Measured on a conservative 40
# headlines/day: after one year the news file holds 14,600 items and 2.9 MB, of
# which only ~5% are recent enough for any analyzer to look at — and every scan
# parses all of it while every write re-serializes all of it. That is the
# "works for a week, then gets slow and weird" failure mode, and it is entirely
# self-inflicted.
#
# Each window is set from what the consuming analyzer actually reads, with
# headroom. Changing an analyzer's lookback means revisiting the matching entry
# here; `tests/test_cache_retention.py` pins the relationship so the two cannot
# silently drift apart.
RETENTION: dict[str, timedelta] = {
    # analyzers/sentiment.py ignores anything older than MAX_AGE_DAYS (21)
    "news": timedelta(days=30),
    # analyzers/crypto_onchain.py reads the last few days; the window is wide
    # so on-chain history stays usable for future factor work
    "onchain": timedelta(days=400),
    # fundamentals are quarterly and tiny (8 rows per asset); keeping several
    # years costs nothing and revenue growth needs 5 quarters
    "fundamentals": timedelta(days=1825),
}

# Events are pruned by a different rule: future events must NEVER be dropped
# (they are the whole point of a calendar), and past ones stop mattering once
# they are behind the analyzer's horizon.
EVENT_PAST_RETENTION = timedelta(days=90)


def _ttl_for(kind: str, interval: str = "") -> timedelta:
    return TTL.get(f"{kind}:{interval}", TTL.get(kind, timedelta(hours=1)))


def prune_collection(kind: str, items: list[Any], now: datetime | None = None) -> list[Any]:
    """Drop rows past their retention window.

    Pure and separately testable, because getting this wrong deletes data an
    analyzer needs and the symptom would be a source that quietly says less than
    it should. Items with no usable timestamp are always kept — refusing to
    delete what we cannot date is the safe direction to fail in.
    """
    now = now or datetime.now(timezone.utc)

    if kind == "events":
        # Never drop a future event. A calendar that forgets tomorrow's FOMC is
        # worse than no calendar at all.
        cutoff = now - EVENT_PAST_RETENTION
        return [i for i in items if getattr(i, "ts", None) is None or i.ts >= cutoff]

    window = RETENTION.get(kind)
    if window is None:
        return items

    cutoff = now - window
    return [i for i in items if getattr(i, "ts", None) is None or i.ts >= cutoff]


def is_stale(fetched_at: datetime, kind: str, interval: str = "") -> bool:
    age = datetime.now(timezone.utc) - fetched_at
    return age > _ttl_for(kind, interval)


def _tmp_path(p: Path) -> Path:
    """Writer-private temp name, unique per process *and* per thread.

    The PID alone is not enough. Two threads in one process share a PID, so they
    build the same temp filename, and the second rename fails with
    FileNotFoundError because the first already moved the file away. That is a
    crash, not a lost update — and `web/server.py` runs a ThreadingHTTPServer,
    so the door is open even though it only reads today.

    Each rename is still atomic, so concurrent writers give last-writer-wins
    without ever leaving a torn file.

    ponytail: last-writer-wins loses concurrent updates to the same bucket. Fine
    for a cache (the next refresh refills it) and for diagnostics. If a caller
    ever needs read-modify-write to be atomic across processes, this needs a
    real lock, not a better temp name.
    """
    return p.with_name(f"{p.name}.{os.getpid()}.{threading.get_ident()}.tmp")


def _warn_corrupt(p: Path, e: Exception) -> None:
    """A cache file that fails to parse is treated as absent (the caller will
    refetch), never as a crash: the cache is a convenience copy of remote
    data, so the honest recovery is a refetch, loudly."""
    print(
        f"[cache] WARNING: corrupt cache file {p} ({type(e).__name__}); refetching", file=sys.stderr
    )


class LocalStore:
    """Zero-dependency file-backed store. JSON for simplicity at this stage;
    swap the serialization for Parquet once series get large. Lives under data/
    so a cloner can inspect exactly what's cached."""

    # Collection kinds stored as append-and-merge JSON lists (Phase 11 data).
    # Each maps to a pydantic model and a function producing the dedup key: two
    # items with the same key are the same fact, and the newer one wins.
    _COLLECTIONS: dict[str, tuple[type[BaseModel], Callable[[Any], str]]] = {
        "news": (NewsItem, lambda i: i.url or f"{i.source}:{i.ts.isoformat()}:{i.headline}"),
        "onchain": (OnChainObservation, lambda i: f"{i.metric}:{i.ts.isoformat()}"),
        "fundamentals": (Fundamentals, lambda i: f"{i.asset}:{i.period}"),
        "events": (EventItem, lambda i: f"{i.region}:{i.name}:{i.ts.isoformat()}"),
    }

    def __init__(self, root: str | Path | None = None) -> None:
        # None -> resolve through config.data_dir(), which honours ALPHA_DATA_DIR
        # so the engine is safe to run from any working directory.
        self.root = Path(root) if root is not None else data_dir() / "cache"
        for sub in ("price", "macro", "chain", *self._COLLECTIONS):
            (self.root / sub).mkdir(parents=True, exist_ok=True)

    def _price_path(self, asset: str, interval: str) -> Path:
        return self.root / "price" / f"{asset.upper()}_{interval}.json"

    def _macro_path(self, series_id: str) -> Path:
        return self.root / "macro" / f"{series_id}.json"

    def _chain_path(self, underlying: str) -> Path:
        return self.root / "chain" / f"{underlying.upper()}.json"

    def write_price(self, series: PriceSeries) -> None:
        p = self._price_path(series.asset, series.interval.value)
        tmp = _tmp_path(p)
        tmp.write_text(series.model_dump_json(indent=2))
        tmp.rename(p)

    def read_price(self, asset: str, interval: str) -> PriceSeries | None:
        p = self._price_path(asset, interval)
        if not p.exists():
            return None
        try:
            return PriceSeries.model_validate_json(p.read_text())
        except Exception as e:  # noqa: BLE001 - corrupt cache = missing cache
            _warn_corrupt(p, e)
            return None

    def write_macro(self, obs: list[MacroObservation]) -> None:
        """Write macro observations, merging with any existing cached data.

        Observations are keyed by (series_id, timestamp). New observations
        replace any existing observation with the same key; older cached
        observations are preserved. This prevents silent data loss when a
        caller passes only a subset of the full series.
        """
        by_series: dict[str, list[MacroObservation]] = {}
        for o in obs:
            by_series.setdefault(o.series_id, []).append(o)
        for series_id, new_items in by_series.items():
            p = self._macro_path(series_id)
            existing: list[MacroObservation] = []
            if p.exists():
                try:
                    raw = json.loads(p.read_text())
                    existing = [MacroObservation.model_validate(r) for r in raw]
                except Exception:  # noqa: BLE001 - corrupted cache, start fresh
                    existing = []
            # Merge: new observations override by timestamp
            merged_by_ts: dict[datetime, MacroObservation] = {}
            for item in existing:
                merged_by_ts[item.ts] = item
            for item in new_items:
                merged_by_ts[item.ts] = item
            merged = sorted(merged_by_ts.values(), key=lambda o: o.ts)
            tmp = _tmp_path(p)
            tmp.write_text(json.dumps([i.model_dump(mode="json") for i in merged], indent=2))
            tmp.rename(p)

    def read_macro(self, series_id: str) -> list[MacroObservation]:
        p = self._macro_path(series_id)
        if not p.exists():
            return []
        try:
            raw = json.loads(p.read_text())
            return [MacroObservation.model_validate(r) for r in raw]
        except Exception as e:  # noqa: BLE001 - corrupt cache = missing cache
            _warn_corrupt(p, e)
            return []

    def macro_fetched_at(self, series_id: str) -> datetime | None:
        """When this series was last written, from the file's mtime.

        Macro is the one kind whose freshness cannot be read off its own data.
        A price series carries `fetched_at`; a macro series is a bare JSON array
        of observations, and the newest observation's *date* is not when we
        fetched it — CPI for June is published in July and is the current print
        all month.

        Using the observation date meant `is_stale` compared a 45-day-old
        datapoint against a 1-day TTL and answered True for a file written one
        second earlier. The macro cache therefore never hit: every equity scan
        refetched all three FRED series, so one `batch` run over four equities
        made twelve identical API calls instead of three.

        The mtime is the honest answer to "when did we last ask FRED", needs no
        change to the on-disk format, and stays correct when a fetch returns
        nothing new (the file is still rewritten, so we did still ask).
        """
        p = self._macro_path(series_id)
        if not p.exists():
            return None
        return datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)

    def write_chain(self, chain: OptionsChain) -> None:
        p = self._chain_path(chain.underlying)
        tmp = _tmp_path(p)
        tmp.write_text(chain.model_dump_json(indent=2))
        tmp.rename(p)

    def read_chain(self, underlying: str) -> OptionsChain | None:
        p = self._chain_path(underlying)
        if not p.exists():
            return None
        try:
            return OptionsChain.model_validate_json(p.read_text())
        except Exception as e:  # noqa: BLE001 - corrupt cache = missing cache
            _warn_corrupt(p, e)
            return None

    # -- Phase 11 collections -------------------------------------------------
    #
    # News, on-chain metrics, fundamentals and calendar events are all
    # "append a batch, merge with what's there, dedup by key" — one
    # implementation rather than four copies with different nouns.

    def _collection_path(self, kind: str, bucket: str) -> Path:
        # Buckets come from source names and tickers, so a traversal-safe
        # filename matters: a source called "../../etc/passwd" must not escape.
        safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in bucket).strip("._")
        return self.root / kind / f"{safe or 'default'}.json"

    def write_collection(self, kind: str, bucket: str, items: list[Any]) -> None:
        """Merge `items` into the bucket's file, newest value winning per key,
        then drop anything past its retention window."""
        model, key_of = self._COLLECTIONS[kind]
        p = self._collection_path(kind, bucket)
        merged: dict[str, Any] = {key_of(i): i for i in self.read_collection(kind, bucket)}
        for item in items:
            merged[key_of(item)] = item
        kept = prune_collection(kind, list(merged.values()))
        ordered = sorted(kept, key=lambda i: getattr(i, "ts", datetime.min))
        tmp = _tmp_path(p)
        tmp.write_text(json.dumps([i.model_dump(mode="json") for i in ordered], indent=2))
        tmp.rename(p)

    def read_collection(self, kind: str, bucket: str) -> list[Any]:
        model, _ = self._COLLECTIONS[kind]
        p = self._collection_path(kind, bucket)
        if not p.exists():
            return []
        try:
            return [model.model_validate(r) for r in json.loads(p.read_text())]
        except Exception as e:  # noqa: BLE001 - corrupt cache = missing cache
            _warn_corrupt(p, e)
            return []

    def collection_fetched_at(self, kind: str, bucket: str) -> datetime | None:
        """File write time, which is not the date of the newest reported fact."""
        p = self._collection_path(kind, bucket)
        return datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc) if p.exists() else None

    def read_all_collection(self, kind: str) -> list[Any]:
        """Every bucket of a kind, concatenated. This is what an analyzer wants:
        all the news, regardless of which feed carried it."""
        model, _ = self._COLLECTIONS[kind]
        out: list[Any] = []
        for p in sorted((self.root / kind).glob("*.json")):
            try:
                out.extend(model.model_validate(r) for r in json.loads(p.read_text()))
            except Exception as e:  # noqa: BLE001 - one bad file must not hide the rest
                _warn_corrupt(p, e)
        return out


class Cache:
    """The public read interface. Analyzers get one of these and ask it for data.
    They never know or care where it came from."""

    def __init__(self, store: LocalStore | None = None) -> None:
        self.store: LocalStore = store or LocalStore()

    def get_price(self, asset: str, interval: str) -> tuple[PriceSeries | None, bool]:
        """Returns (series, stale). series is None if nothing cached yet.
        stale=True means it exists but exceeded its TTL; caller may refresh."""
        series = self.store.read_price(asset, interval)
        if series is None:
            return None, True
        return series, is_stale(series.fetched_at, "price", interval)

    def get_macro(self, series_id: str) -> tuple[list[MacroObservation], bool]:
        """Returns (observations, stale).

        Staleness is measured from when the series was last FETCHED, not from
        the date of its newest observation — see `LocalStore.macro_fetched_at`
        for why that distinction is the difference between a working cache and
        one that never hits.
        """
        obs = self.store.read_macro(series_id)
        if not obs:
            return [], True
        fetched_at = self.store.macro_fetched_at(series_id)
        if fetched_at is None:
            return obs, True
        return obs, is_stale(fetched_at, "macro")

    def get_chain(self, underlying: str) -> tuple[OptionsChain | None, bool]:
        """Returns (chain, stale). Same contract as get_price: None means
        nothing cached; stale=True means it exists but exceeded its TTL."""
        chain = self.store.read_chain(underlying)
        if chain is None:
            return None, True
        return chain, is_stale(chain.fetched_at, "chain")

    def put_price(self, series: PriceSeries) -> None:
        self.store.write_price(series)

    def put_macro(self, obs: list[MacroObservation]) -> None:
        self.store.write_macro(obs)

    def put_chain(self, chain: OptionsChain) -> None:
        self.store.write_chain(chain)

    # -- Phase 11 collections -------------------------------------------------

    def _get_collection(self, kind: str, bucket: str | None) -> tuple[list[Any], bool]:
        """Read one bucket or every bucket, plus whether the newest item is
        stale. Staleness is judged on the newest `ts` present: a feed with no
        items at all is stale by definition, which is what triggers a fetch."""
        items = (
            self.store.read_all_collection(kind)
            if bucket is None
            else self.store.read_collection(kind, bucket)
        )
        if not items:
            return [], True
        newest = max(i.ts for i in items)
        return items, is_stale(newest, kind)

    def get_news(self, source: str | None = None) -> tuple[list[NewsItem], bool]:
        return self._get_collection("news", source)

    def put_news(self, source: str, items: list[NewsItem]) -> None:
        self.store.write_collection("news", source, items)

    def get_onchain(self, metric: str | None = None) -> tuple[list[OnChainObservation], bool]:
        return self._get_collection("onchain", metric)

    def put_onchain(self, metric: str, items: list[OnChainObservation]) -> None:
        self.store.write_collection("onchain", metric, items)

    def get_fundamentals(self, asset: str) -> tuple[list[Fundamentals], bool]:
        asset = asset.upper()
        items = self.store.read_collection("fundamentals", asset)
        if not items:
            return [], True
        fetched_at = self.store.collection_fetched_at("fundamentals", asset)
        return items, fetched_at is None or is_stale(fetched_at, "fundamentals")

    def put_fundamentals(self, asset: str, items: list[Fundamentals]) -> None:
        self.store.write_collection("fundamentals", asset.upper(), items)

    def get_events(self, region: str | None = None) -> tuple[list[EventItem], bool]:
        return self._get_collection("events", region)

    def put_events(self, region: str, items: list[EventItem]) -> None:
        self.store.write_collection("events", region, items)
