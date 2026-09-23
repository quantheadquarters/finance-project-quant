"""The SEC adapter must use original filing dates and real quarterly amounts."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from alpha_engine.cache.interface import Cache, LocalStore
from alpha_engine.ingestion import sec_fundamentals


def _fact(start, end, value, filed, fp="Q2", fy=2026, form="10-Q"):
    row = {"end": end, "val": value, "filed": filed, "form": form, "fp": fp, "fy": fy}
    if start is not None:
        row["start"] = start
    return row


def _companyfacts():
    start_2026 = "2025-09-28"
    q1 = "2025-12-27"
    q2 = "2026-03-28"
    prior_q2 = "2025-03-29"

    def usd(rows):
        return {"units": {"USD": rows}}

    return {
        "entityName": "Apple Inc.",
        "facts": {
            "us-gaap": {
                "RevenueFromContractWithCustomerExcludingAssessedTax": usd(
                    [
                        _fact("2024-12-29", prior_q2, 100, "2025-05-01", fy=2025),
                        _fact("2024-12-29", prior_q2, 999, "2026-05-01", fy=2025),
                        _fact(start_2026, q1, 120, "2026-01-30", fp="Q1"),
                        _fact("2025-12-28", q2, 130, "2026-05-01"),
                    ]
                ),
                "NetIncomeLoss": usd([_fact("2025-12-28", q2, 25, "2026-05-01")]),
                "GrossProfit": usd([_fact("2025-12-28", q2, 65, "2026-05-01")]),
                "NetCashProvidedByUsedInOperatingActivities": usd(
                    [
                        _fact(start_2026, q1, 30, "2026-01-30", fp="Q1"),
                        _fact(start_2026, q2, 65, "2026-05-01"),
                    ]
                ),
                "LongTermDebtCurrent": usd([_fact(None, q2, 10, "2026-05-01")]),
                "LongTermDebtNoncurrent": usd([_fact(None, q2, 40, "2026-05-01")]),
                "StockholdersEquity": usd([_fact(None, q2, 100, "2026-05-01")]),
            }
        },
    }


def test_sec_original_filing_quarterly_cash_flow_and_publication_gate():
    rows = sec_fundamentals.parse_companyfacts(_companyfacts())
    assert [r.period for r in rows] == ["2025-Q2", "2026-Q1", "2026-Q2"]
    assert rows[0].revenue == 100  # later restatement must not replace the first filing
    latest = rows[-1]
    assert latest.available_at == datetime(2026, 5, 2, tzinfo=timezone.utc)
    assert latest.revenue == 130
    assert latest.net_income == 25
    assert latest.operating_cash_flow == 35  # 65 YTD minus 30 in Q1
    assert latest.gross_margin == pytest.approx(0.5)
    assert latest.total_debt == 50
    assert latest.total_equity == 100


def test_sec_derives_q4_from_first_filed_annual_and_nine_month_amounts():
    data = _companyfacts()
    gaap = data["facts"]["us-gaap"]
    start = "2025-09-28"
    q3 = "2026-06-27"
    year_end = "2026-09-26"
    gaap["RevenueFromContractWithCustomerExcludingAssessedTax"]["units"]["USD"].extend(
        [
            _fact(start, q3, 300, "2026-07-31", fp="Q3"),
            _fact(start, year_end, 450, "2026-11-01", fp="FY", form="10-K"),
        ]
    )
    gaap["NetCashProvidedByUsedInOperatingActivities"]["units"]["USD"].extend(
        [
            _fact(start, q3, 90, "2026-07-31", fp="Q3"),
            _fact(start, year_end, 120, "2026-11-01", fp="FY", form="10-K"),
        ]
    )
    rows = sec_fundamentals.parse_companyfacts(data)
    q4 = next(row for row in rows if row.period == "2026-Q4")
    assert q4.revenue == 150
    assert q4.operating_cash_flow == 30
    assert q4.available_at == datetime(2026, 11, 2, tzinfo=timezone.utc)


def test_sec_wrong_response_fails_loudly(capsys):
    assert sec_fundamentals.parse_companyfacts({"entityName": "Not Apple"}) == []
    assert "CONTRACT BROKEN" in capsys.readouterr().err
    assert sec_fundamentals.parse_companyfacts({"entityName": "Apple Inc.", "facts": []}) == []
    assert "CONTRACT BROKEN" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("asset", "entity", "tag"),
    [
        ("AAPL", "Apple Inc.", "RevenueFromContractWithCustomerExcludingAssessedTax"),
        ("MSFT", "MICROSOFT CORPORATION", "RevenueFromContractWithCustomerExcludingAssessedTax"),
        ("GOOGL", "Alphabet Inc.", "Revenues"),
        ("NVDA", "NVIDIA CORP", "Revenues"),
    ],
)
def test_sec_verified_company_mapping(asset, entity, tag, capsys):
    data = _companyfacts()
    data["entityName"] = entity
    revenue = data["facts"]["us-gaap"].pop("RevenueFromContractWithCustomerExcludingAssessedTax")
    data["facts"]["us-gaap"][tag] = revenue
    assert len(sec_fundamentals.parse_companyfacts(data, asset)) == 3
    data["entityName"] = "Wrong company"
    assert sec_fundamentals.parse_companyfacts(data, asset) == []
    assert "CONTRACT BROKEN" in capsys.readouterr().err


def test_alphabet_revenue_tag_transition_keeps_older_quarters():
    data = _companyfacts()
    data["entityName"] = "Alphabet Inc."
    gaap = data["facts"]["us-gaap"]
    older = gaap["RevenueFromContractWithCustomerExcludingAssessedTax"]
    gaap["Revenues"] = {"units": {"USD": [older["units"]["USD"][-1]]}}
    rows = sec_fundamentals.parse_companyfacts(data, "GOOGL")
    assert [r.period for r in rows] == ["2025-Q2", "2026-Q1", "2026-Q2"]


def test_sec_fetch_uses_declared_agent_and_caches(monkeypatch, tmp_path):
    class Response:
        status_code = 200

        def json(self):
            return _companyfacts()

    def fake_get(url, *, headers, timeout):
        assert url.endswith("CIK0000320193.json")
        assert headers == {"User-Agent": "Researcher contact@example.com"}
        return Response()

    monkeypatch.setenv("SEC_USER_AGENT", "Researcher contact@example.com")
    monkeypatch.setenv("ALPHA_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(sec_fundamentals.net, "get", fake_get)
    cache = Cache(LocalStore(tmp_path / "cache"))
    rows = sec_fundamentals.fetch_fundamentals("AAPL", cache)
    assert len(rows) == 3
    assert len(cache.get_fundamentals("AAPL")[0]) == 3


def test_context_refresh_uses_sec_when_fmp_key_is_absent(monkeypatch, tmp_path):
    from alpha_engine.ingestion import fmp
    from alpha_engine.orchestrator.engine import refresh_context

    monkeypatch.setenv("ALPHA_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(fmp, "has_key", lambda: False)
    monkeypatch.setattr(sec_fundamentals, "has_user_agent", lambda: True)
    monkeypatch.setattr(
        sec_fundamentals,
        "fetch_fundamentals",
        lambda asset, cache: sec_fundamentals.parse_companyfacts(_companyfacts(), asset),
    )
    report = refresh_context(Cache(LocalStore(tmp_path / "cache")), ("AAPL",), {"fundamentals"})
    assert report.item_counts["fundamentals.sec_aapl"] == 3
    assert "fundamentals" in report.refreshed
