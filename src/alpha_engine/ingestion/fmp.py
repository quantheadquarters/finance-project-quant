"""Financial Modeling Prep — company fundamentals, key-gated.

Fundamentals are the biggest domain the engine was missing. This adapter pulls
income-statement, balance-sheet and cash-flow figures and normalizes them into
`Fundamentals` rows, one per reporting period.

What it deliberately does NOT do: derive a valuation. DCF and comparables both
require assumptions (discount rate, terminal growth, peer set) that are judgment
calls dressed as arithmetic. FUTURE_WORK defers them on purpose and this adapter
respects that — it reports what the filings said, nothing more.

Gating: no `FMP_API_KEY` means unavailable, and the equity path proceeds on
price structure alone.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from typing import Any

from alpha_engine import net
from alpha_engine.cache.interface import Cache
from alpha_engine.cache.models import Fundamentals
from alpha_engine.config import load_project_env

_BASE = "https://financialmodelingprep.com/api/v3"
SOURCE = "fmp"


def has_key() -> bool:
    load_project_env()
    return bool(os.environ.get("FMP_API_KEY"))


def _num(row: dict[str, Any], *keys: str) -> float | None:
    """First present, numeric value among `keys`. Missing stays missing: a
    zero margin and an unknown margin support opposite conclusions."""
    for key in keys:
        value = row.get(key)
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _date(row: dict[str, Any], *keys: str) -> datetime | None:
    """First usable FMP timestamp, normalized to UTC."""
    for key in keys:
        raw = row.get(key)
        if not raw:
            continue
        try:
            parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _fetch(endpoint: str, asset: str, limit: int) -> list[dict[str, Any]]:
    try:
        resp = net.get(
            f"{_BASE}/{endpoint}/{asset}",
            params={"limit": str(limit), "apikey": os.environ["FMP_API_KEY"]},
            timeout=20,
        )
        if resp.status_code >= 400:
            print(f"[fmp] {endpoint} {asset}: HTTP {resp.status_code}", file=sys.stderr)
            return []
        rows = resp.json()
        return rows if isinstance(rows, list) else []
    except Exception as e:  # noqa: BLE001 - fundamentals are optional context
        print(f"[fmp] {endpoint} {asset} failed: {e}", file=sys.stderr)
        return []


def fetch_fundamentals(
    asset: str,
    limit: int = 8,
    cache: Cache | None = None,
) -> list[Fundamentals]:
    """Fetch the last `limit` quarterly periods, merged across the three
    statements. Empty list on no key or any failure."""
    if not has_key():
        print(
            "[fmp] FMP_API_KEY not set; skipping fundamentals "
            "(free key: https://financialmodelingprep.com)",
            file=sys.stderr,
        )
        return []

    asset = asset.upper()
    income = _fetch("income-statement", asset, limit)
    balance = _fetch("balance-sheet-statement", asset, limit)
    cashflow = _fetch("cash-flow-statement", asset, limit)

    if not income:
        return []

    # Index the slower-moving statements by period so a missing balance sheet
    # for one quarter does not drop that quarter's income data.
    by_period_balance = {r.get("period") + str(r.get("calendarYear")): r for r in balance}
    by_period_cash = {r.get("period") + str(r.get("calendarYear")): r for r in cashflow}

    out: list[Fundamentals] = []
    for row in income:
        period_key = str(row.get("period") or "") + str(row.get("calendarYear") or "")
        bal = by_period_balance.get(period_key, {})
        cash = by_period_cash.get(period_key, {})

        try:
            ts = datetime.fromisoformat(str(row["date"])).replace(tzinfo=timezone.utc)
        except (KeyError, ValueError):
            continue

        revenue = _num(row, "revenue")
        gross_profit = _num(row, "grossProfit")
        out.append(
            Fundamentals(
                asset=asset,
                period=f"{row.get('calendarYear')}-{row.get('period')}",
                ts=ts,
                # `date` is the quarter end, not the day investors learned the
                # numbers. Backtests must gate on the filing timestamp or abstain.
                available_at=_date(row, "acceptedDate", "fillingDate", "filingDate"),
                revenue=revenue,
                net_income=_num(row, "netIncome"),
                operating_cash_flow=_num(
                    cash, "operatingCashFlow", "netCashProvidedByOperatingActivities"
                ),
                gross_margin=(
                    gross_profit / revenue if gross_profit is not None and revenue else None
                ),
                total_debt=_num(bal, "totalDebt"),
                total_equity=_num(bal, "totalStockholdersEquity", "totalEquity"),
                shares_outstanding=_num(row, "weightedAverageShsOut"),
                source=SOURCE,
            )
        )

    if out and cache is not None:
        cache.put_fundamentals(asset, out)
    return out
