"""FRED ingestion adapter for US macro series (CPI, fed funds, unemployment).

This is the first key-gated source in the project, and the gating rule from the
plan applies: the key is free (https://fred.stlouisfed.org), it is read from the
environment, and NOTHING in the default path requires it. A missing key raises
`MissingAPIKeyError` with instructions; callers (the CLI) catch it and degrade
gracefully to a trend-only signal rather than crashing.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

from alpha_engine import net
from alpha_engine.cache.interface import Cache
from alpha_engine.cache.models import MacroObservation

_BASE = "https://api.stlouisfed.org/fred/series/observations"

# The default macro dashboard: policy rate, inflation, labor. Enough to read
# the tightening-vs-easing posture without pretending to be a macro desk.
MACRO_SERIES: tuple[str, ...] = ("FEDFUNDS", "CPIAUCSL", "UNRATE")


class MissingAPIKeyError(RuntimeError):
    """Raised when a key-gated source is called without its key. Deliberately
    its own type so the CLI can catch exactly this and degrade gracefully."""


def _parse_observations(series_id: str, payload: dict) -> list[MacroObservation]:
    """Normalize FRED's observations JSON. Pure function, unit-testable offline.
    FRED encodes missing datapoints as value '.'; those are skipped, not zeroed."""
    out: list[MacroObservation] = []
    for row in payload.get("observations", []):
        raw = row.get("value", ".")
        if raw == ".":
            continue
        out.append(
            MacroObservation(
                series_id=series_id,
                ts=datetime.strptime(row["date"], "%Y-%m-%d").replace(tzinfo=timezone.utc),
                value=float(raw),
                source="fred",
            )
        )
    return out


def fetch_series(
    series_id: str,
    api_key: str | None = None,
    cache: Cache | None = None,
    limit: int = 120,
) -> list[MacroObservation]:
    """Fetch the most recent observations of one FRED series, normalize, cache,
    and return them (oldest first). ~120 monthly points is a decade: plenty for
    year-over-year and trend reads without hoarding."""
    cache = cache or Cache()
    key = api_key or os.environ.get("FRED_API_KEY")
    if not key:
        raise MissingAPIKeyError(
            "FRED_API_KEY not set. Get a free key at https://fred.stlouisfed.org "
            "and export it (see .env.example). Macro context is skipped without it."
        )

    try:
        resp = net.get(
            _BASE,
            params={
                "series_id": series_id,
                "api_key": key,
                "file_type": "json",
                "sort_order": "desc",
                "limit": str(limit),
            },
            timeout=20,
        )
        resp.raise_for_status()
        obs = _parse_observations(series_id, resp.json())
    except Exception as exc:
        # net.Response includes the URL in errors, and FRED puts its key in the URL.
        raise RuntimeError(f"FRED {series_id}: {str(exc).replace(key, '[REDACTED]')}") from None
    # write_macro merges and sorts by timestamp internally; no pre-sort needed here.

    cache.put_macro(obs)
    return obs
