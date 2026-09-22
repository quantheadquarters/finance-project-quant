"""Finnhub company news — key-gated, company-tagged headlines.

The RSS feeds in `ingestion/rss.py` carry regulator and central-bank
announcements. What they do not carry is "news about AAPL specifically", which
is what a per-asset sentiment read actually wants. Finnhub's free tier does,
so this adapter exists to fill that hole for anyone willing to get a free key.

Gating: with no `FINNHUB_API_KEY` the module reports itself unavailable and the
pipeline proceeds without it. The keyless path stays the default path.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from alpha_engine import net
from alpha_engine.cache.interface import Cache
from alpha_engine.cache.models import NewsItem
from alpha_engine.config import data_dir, load_project_env
from alpha_engine.ingestion.rss import tag_assets

_BASE = "https://finnhub.io/api/v1"
SOURCE = "finnhub"


def has_key() -> bool:
    load_project_env()
    return bool(os.environ.get("FINNHUB_API_KEY"))


def fetch_company_news(
    asset: str,
    days: int = 14,
    cache: Cache | None = None,
    *,
    store: bool = True,
    end_date: date | None = None,
) -> list[NewsItem]:
    """Fetch recent company news for one ticker.

    Returns an empty list (never raises) when the key is absent or the request
    fails: news is optional context and must never take a scan down.
    """
    if not has_key():
        print(
            "[finnhub] FINNHUB_API_KEY not set; skipping company news "
            "(free key: https://finnhub.io)",
            file=sys.stderr,
        )
        return []

    if days < 1:
        raise ValueError("days must be positive")
    if store:
        cache = cache or Cache()
    asset = asset.upper()
    today = end_date or datetime.now(timezone.utc).date()

    try:
        resp = net.get(
            f"{_BASE}/company-news",
            params={
                "symbol": asset,
                "from": (today - timedelta(days=days)).isoformat(),
                "to": today.isoformat(),
                "token": os.environ["FINNHUB_API_KEY"],
            },
            timeout=20,
        )
        if resp.status_code >= 400:
            print(f"[finnhub] HTTP {resp.status_code} for {asset}", file=sys.stderr)
            return []
        rows = resp.json()
    except Exception as e:  # noqa: BLE001 - optional context, never fatal
        print(f"[finnhub] fetch failed for {asset}: {e}", file=sys.stderr)
        return []

    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        print(f"[finnhub] CONTRACT BROKEN: expected news array for {asset}", file=sys.stderr)
        return []

    items: list[NewsItem] = []
    for row in rows:
        headline = str(row.get("headline") or "").strip()
        if not headline:
            continue
        ts_raw = row.get("datetime")
        try:
            ts = datetime.fromtimestamp(float(ts_raw), tz=timezone.utc)
        except (TypeError, ValueError):
            continue
        # The queried ticker is always a tag; the lexicon may find more.
        tags = sorted({asset, *tag_assets(headline)})
        items.append(
            NewsItem(
                ts=ts,
                headline=headline,
                source=SOURCE,
                url=str(row.get("url") or ""),
                asset_tags=tags,
            )
        )

    if items and store:
        assert cache is not None
        cache.put_news(f"{SOURCE}_{asset}", items)
    return items


def main() -> int:
    """Export an unpruned research archive; the live news cache keeps only 30 days."""
    parser = argparse.ArgumentParser(description="Export Finnhub company headlines for research")
    parser.add_argument("asset", nargs="?", default="AAPL")
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not has_key():
        parser.error("set FINNHUB_API_KEY in .env; do not paste it into chat")
    if args.days < 1:
        parser.error("--days must be positive")
    asset = args.asset.upper()
    requested_to = datetime.now(timezone.utc).date()
    requested_from = requested_to - timedelta(days=args.days)
    items = fetch_company_news(asset, days=args.days, store=False, end_date=requested_to)
    if not items:
        parser.error("no headlines returned; archive not written (check API access/range)")
    output = args.output or data_dir() / "research" / f"{asset}_finnhub_news.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    archive = {
        "asset": asset,
        "provider": SOURCE,
        "requested_from": requested_from.isoformat(),
        "requested_to": requested_to.isoformat(),
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "items": [item.model_dump(mode="json") for item in items],
    }
    try:
        with output.open("x") as file:
            json.dump(archive, file, indent=2)
    except FileExistsError:
        parser.error(f"{output} already exists; choose a new --output to preserve the archive")
    dates = [item.ts for item in items]
    print(f"{len(items)} headlines: {min(dates).date()} to {max(dates).date()} -> {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
