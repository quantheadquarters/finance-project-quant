"""Verified US-equity quarterly fundamentals from the SEC company-facts API.

Direct quarter-length income facts are used for Q1-Q3. Q4 comes from the
first-filed annual amount minus the first-filed nine-month amount. SEC cash
flow is usually year-to-date, so its quarterly value is likewise the
difference between consecutive filings from the same fiscal year.
"""

from __future__ import annotations

import os
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Any

from alpha_engine import net
from alpha_engine.cache.interface import Cache
from alpha_engine.cache.models import Fundamentals
from alpha_engine.config import load_project_env

SOURCE = "sec_companyfacts"
_CONTRACT_REVENUE = "RevenueFromContractWithCustomerExcludingAssessedTax"
_COMPANIES = {
    "AAPL": ("0000320193", "Apple Inc.", (_CONTRACT_REVENUE,)),
    "MSFT": ("0000789019", "MICROSOFT CORPORATION", (_CONTRACT_REVENUE,)),
    # Alphabet moved from the contract tag to Revenues; retaining both avoids
    # quietly dropping its entire earlier backtest history.
    "GOOGL": ("0001652044", "Alphabet Inc.", ("Revenues", _CONTRACT_REVENUE)),
    "NVDA": ("0001045810", "NVIDIA CORP", ("Revenues",)),
}
_BASE = "https://data.sec.gov/api/xbrl/companyfacts"
_FORMS = {"10-Q", "10-K"}


def supports(asset: str) -> bool:
    """Only accept tickers whose CIK, name, and revenue tag were checked."""
    return asset.upper() in _COMPANIES


def has_user_agent() -> bool:
    load_project_env()
    return bool(os.environ.get("SEC_USER_AGENT"))


def _facts(data: dict[str, Any], tag: str, *, quarterly: bool = False) -> dict[str, dict]:
    """Keep each period's first public filing, never a later restatement."""
    rows = data.get("facts", {}).get("us-gaap", {}).get(tag, {}).get("units", {}).get("USD", [])
    if not isinstance(rows, list):
        return {}
    selected: dict[str, dict] = {}
    for row in rows:
        if not isinstance(row, dict) or row.get("form") not in _FORMS:
            continue
        if not isinstance(row.get("val"), (int, float)):
            continue
        try:
            end = date.fromisoformat(row["end"])
            filed = date.fromisoformat(row["filed"])
            start = date.fromisoformat(row["start"]) if "start" in row else None
        except (KeyError, TypeError, ValueError):
            continue
        if quarterly and (start is None or not 75 <= (end - start).days <= 110):
            continue
        key = f"{start}:{end}" if start else str(end)
        if key not in selected or filed < date.fromisoformat(selected[key]["filed"]):
            selected[key] = row
    return selected


def _quarter_facts(data: dict[str, Any], tag: str) -> dict[str, dict]:
    """Add Q4 only when annual and nine-month facts share a fiscal start."""
    direct = _facts(data, tag, quarterly=True)
    all_rows = _facts(data, tag)
    for annual in all_rows.values():
        if annual.get("form") != "10-K" or annual.get("fp") != "FY":
            continue
        start = annual.get("start")
        end = annual["end"]
        if (
            not start
            or not 330 <= (date.fromisoformat(end) - date.fromisoformat(start)).days <= 380
        ):
            continue
        if any(row["end"] == end for row in direct.values()):
            continue
        earlier = [
            row
            for row in all_rows.values()
            if row.get("start") == start
            and row["end"] < end
            and row.get("form") == "10-Q"
            and 240 <= (date.fromisoformat(row["end"]) - date.fromisoformat(start)).days <= 300
            and row["filed"] <= annual["filed"]
        ]
        if not earlier:
            continue
        nine_month = max(earlier, key=lambda row: row["end"])
        quarter_start = (date.fromisoformat(nine_month["end"]) + timedelta(days=1)).isoformat()
        key = f"{quarter_start}:{end}"
        direct[key] = {
            **annual,
            "start": quarter_start,
            "val": annual["val"] - nine_month["val"],
            "fp": "Q4",
        }
    return direct


def parse_companyfacts(data: dict[str, Any], asset: str = "AAPL") -> list[Fundamentals]:
    """Normalize SEC facts while retaining only what was filed by each period."""
    asset = asset.upper()
    if asset not in _COMPANIES:
        raise ValueError(f"unsupported SEC ticker: {asset}")
    _, entity_name, revenue_tags = _COMPANIES[asset]
    if not isinstance(data, dict) or data.get("entityName") != entity_name:
        print(f"[sec] CONTRACT BROKEN: unexpected {asset} company-facts response", file=sys.stderr)
        return []
    facts = data.get("facts")
    gaap = facts.get("us-gaap") if isinstance(facts, dict) else None
    if not isinstance(gaap, dict) or not any(tag in gaap for tag in revenue_tags):
        print(f"[sec] CONTRACT BROKEN: {asset} revenue facts missing", file=sys.stderr)
        return []

    revenue: dict[str, dict] = {}
    for tag in revenue_tags:
        for key, row in _quarter_facts(data, tag).items():
            revenue.setdefault(key, row)
    if not revenue:
        print(f"[sec] CONTRACT BROKEN: no usable quarterly {asset} revenue", file=sys.stderr)
        return []
    income = _quarter_facts(data, "NetIncomeLoss")
    gross = _quarter_facts(data, "GrossProfit")
    cash = _facts(data, "NetCashProvidedByUsedInOperatingActivities")
    debt_current = _facts(data, "LongTermDebtCurrent")
    debt_noncurrent = _facts(data, "LongTermDebtNoncurrent")
    equity = _facts(data, "StockholdersEquity")
    out: list[Fundamentals] = []

    for key, row in sorted(revenue.items(), key=lambda pair: pair[1]["end"]):
        period = str(row.get("fp") or "")
        if period not in {"Q1", "Q2", "Q3", "Q4"}:
            continue
        filed = date.fromisoformat(row["filed"])
        end = row["end"]

        def filed_value(facts: dict[str, dict], fact_key: str) -> float | None:
            fact = facts.get(fact_key)
            return (
                float(fact["val"]) if fact and date.fromisoformat(fact["filed"]) <= filed else None
            )

        net_income = filed_value(income, key)
        gross_profit = filed_value(gross, key)
        debt_now = filed_value(debt_current, end)
        debt_later = filed_value(debt_noncurrent, end)
        total_debt = (
            debt_now + debt_later if debt_now is not None and debt_later is not None else None
        )

        # Cash flow statements are cumulative from the fiscal-year start.
        # Subtract the previous quarter's cumulative value when available.
        ytd = [
            fact
            for fact in cash.values()
            if fact.get("end") == end and date.fromisoformat(fact["filed"]) <= filed
        ]
        operating_cash_flow = None
        if ytd:
            current = min(ytd, key=lambda fact: fact["start"])
            previous = [
                fact
                for fact in cash.values()
                if fact.get("start") == current["start"]
                and fact["end"] < end
                and date.fromisoformat(fact["filed"]) <= filed
            ]
            if previous:
                prior = max(previous, key=lambda fact: fact["end"])
                if 75 <= (date.fromisoformat(end) - date.fromisoformat(prior["end"])).days <= 110:
                    operating_cash_flow = float(current["val"] - prior["val"])
            elif 75 <= (date.fromisoformat(end) - date.fromisoformat(current["start"])).days <= 110:
                operating_cash_flow = float(current["val"])

        sales = float(row["val"])
        out.append(
            Fundamentals(
                asset=asset.upper(),
                period=f"{row['fy']}-{period}",
                ts=datetime.combine(date.fromisoformat(end), datetime.min.time(), timezone.utc),
                # SEC gives a filing date, not a market-time publication stamp.
                # Next UTC day is conservative for a daily-bar replay.
                available_at=datetime.combine(
                    filed + timedelta(days=1), datetime.min.time(), timezone.utc
                ),
                revenue=sales,
                net_income=net_income,
                operating_cash_flow=operating_cash_flow,
                gross_margin=gross_profit / sales
                if gross_profit is not None and sales > 0
                else None,
                total_debt=total_debt,
                total_equity=filed_value(equity, end),
                source=SOURCE,
            )
        )
    return out


def fetch_fundamentals(asset: str, cache: Cache | None = None) -> list[Fundamentals]:
    """Fetch one verified SEC ticker; optional context never breaks a scan."""
    asset = asset.upper()
    if not supports(asset) or not has_user_agent():
        return []
    try:
        response = net.get(
            f"{_BASE}/CIK{_COMPANIES[asset][0]}.json",
            headers={"User-Agent": os.environ["SEC_USER_AGENT"]},
            timeout=30,
        )
        if response.status_code >= 400:
            raise ValueError(f"HTTP {response.status_code}")
        periods = parse_companyfacts(response.json(), asset)
    except Exception as exc:  # noqa: BLE001 - SEC is optional context
        print(f"[sec] {asset} fundamentals failed: {exc}", file=sys.stderr)
        return []
    # refresh_context records this asset's SEC feed with its item count.
    if periods and cache is not None:
        cache.put_fundamentals(asset, periods)
    return periods
