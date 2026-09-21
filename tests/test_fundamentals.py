"""Tests for Phase 11c: fundamentals ingestion and analysis.

The NSE scraper tests are the important ones here, and they test the *failure*
path more than the success path. A scraper that breaks loudly is useful; a
scraper that breaks quietly poisons every downstream weight, which is why
`_contract_broken` has more tests than the happy path does.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from alpha_engine.analyzers.fundamentals import (
    MAX_WEIGHT,
    accrual_ratio,
    analyze_fundamentals,
    leverage_ratio,
    revenue_growth,
)
from alpha_engine.cache.interface import Cache, LocalStore
from alpha_engine.cache.models import Fundamentals
from alpha_engine.ingestion import fmp, nse_disclosures
from alpha_engine.schema.signal import Direction

T0 = datetime(2025, 1, 1, tzinfo=timezone.utc)


def _period(idx: int = 0, **kw) -> Fundamentals:
    base = {
        "asset": "TEST",
        "period": f"2025-Q{idx + 1}",
        "ts": T0 + timedelta(days=90 * idx),
        "revenue": 1000.0,
        "net_income": 100.0,
        "operating_cash_flow": 100.0,
        "total_debt": 500.0,
        "total_equity": 1000.0,
    }
    base.update(kw)
    return Fundamentals(**base)


# ---------------------------------------------------------------------------
# Ratios — each must abstain rather than invent
# ---------------------------------------------------------------------------


def test_accrual_ratio_basic():
    assert accrual_ratio(_period(net_income=100.0, operating_cash_flow=130.0)) == pytest.approx(1.3)


def test_accrual_ratio_none_when_inputs_missing():
    assert accrual_ratio(_period(operating_cash_flow=None)) is None
    assert accrual_ratio(_period(net_income=None)) is None


def test_accrual_ratio_none_on_loss():
    """The ratio inverts its meaning with a negative denominator, so it must
    not be computed for a loss-making quarter."""
    assert accrual_ratio(_period(net_income=-50.0)) is None


def test_leverage_ratio_basic():
    assert leverage_ratio(_period(total_debt=500.0, total_equity=1000.0)) == pytest.approx(0.5)


def test_leverage_ratio_none_on_negative_equity():
    assert leverage_ratio(_period(total_equity=-100.0)) is None


def test_revenue_growth_year_over_year():
    periods = [_period(i, revenue=r) for i, r in enumerate([1000, 1000, 1000, 1000, 1200])]
    assert revenue_growth(periods) == pytest.approx(0.2)


def test_revenue_growth_falls_back_with_short_history():
    periods = [_period(0, revenue=1000.0), _period(1, revenue=1100.0)]
    assert revenue_growth(periods) == pytest.approx(0.1)


def test_revenue_growth_none_with_one_period():
    assert revenue_growth([_period()]) is None


def test_revenue_growth_ignores_missing_revenue():
    assert revenue_growth([_period(0, revenue=None), _period(1, revenue=None)]) is None


# ---------------------------------------------------------------------------
# The analyzer
# ---------------------------------------------------------------------------


def test_no_data_is_zero_weight():
    src = analyze_fundamentals([])
    assert src.direction is Direction.NEUTRAL
    assert src.weight == 0.0


def test_all_ratios_missing_is_zero_weight():
    """Data arriving is not the same as data being usable."""
    empty = _period(net_income=None, operating_cash_flow=None, total_equity=None, revenue=None)
    src = analyze_fundamentals([empty])
    assert src.weight == 0.0
    assert "no computable ratios" in src.detail


def test_clean_earnings_low_debt_growth_is_bullish():
    periods = [
        _period(i, revenue=r, operating_cash_flow=150.0, total_debt=100.0, total_equity=1000.0)
        for i, r in enumerate([1000, 1000, 1000, 1000, 1300])
    ]
    src = analyze_fundamentals(periods)
    assert src.direction is Direction.BULLISH
    assert src.weight > 0


def test_accrual_gap_high_debt_contraction_is_bearish():
    periods = [
        _period(i, revenue=r, operating_cash_flow=50.0, total_debt=3000.0, total_equity=1000.0)
        for i, r in enumerate([1000, 1000, 1000, 1000, 900])
    ]
    assert analyze_fundamentals(periods).direction is Direction.BEARISH


def test_mixed_signals_land_neutral():
    periods = [
        _period(i, revenue=r, operating_cash_flow=150.0, total_debt=3000.0, total_equity=1000.0)
        for i, r in enumerate([1000, 1000, 1000, 1000, 1000])
    ]
    assert analyze_fundamentals(periods).direction is Direction.NEUTRAL


def test_weight_respects_the_cap():
    periods = [
        _period(i, revenue=r, operating_cash_flow=500.0, total_debt=1.0, total_equity=1000.0)
        for i, r in enumerate([1000, 1000, 1000, 1000, 5000])
    ]
    assert analyze_fundamentals(periods).weight <= MAX_WEIGHT


def test_more_ratios_gives_more_weight():
    one = analyze_fundamentals(
        [_period(operating_cash_flow=150.0, total_equity=None, revenue=None)]
    )
    three = analyze_fundamentals(
        [
            _period(i, revenue=r, operating_cash_flow=150.0, total_debt=100.0)
            for i, r in enumerate([1000, 1000, 1000, 1000, 1300])
        ]
    )
    assert three.weight > one.weight


def test_analyzer_is_deterministic():
    periods = [_period(i) for i in range(5)]
    a, b = analyze_fundamentals(periods), analyze_fundamentals(periods)
    assert (a.direction, a.weight, a.detail) == (b.direction, b.weight, b.detail)


# ---------------------------------------------------------------------------
# FMP gating
# ---------------------------------------------------------------------------


def test_fmp_without_key_returns_empty(monkeypatch):
    monkeypatch.setattr(fmp, "has_key", lambda: False)
    assert fmp.fetch_fundamentals("AAPL") == []


def test_fmp_parses_and_merges_statements(monkeypatch, tmp_path):
    income = [
        {
            "date": "2025-03-31",
            "acceptedDate": "2025-05-01 16:05:00",
            "period": "Q1",
            "calendarYear": "2025",
            "revenue": 1000,
            "grossProfit": 400,
            "netIncome": 100,
            "weightedAverageShsOut": 50,
        }
    ]
    balance = [
        {"period": "Q1", "calendarYear": "2025", "totalDebt": 200, "totalStockholdersEquity": 800}
    ]
    cash = [{"period": "Q1", "calendarYear": "2025", "operatingCashFlow": 120}]

    def fake_fetch(endpoint, asset, limit):
        return {
            "income-statement": income,
            "balance-sheet-statement": balance,
            "cash-flow-statement": cash,
        }[endpoint]

    monkeypatch.setattr(fmp, "has_key", lambda: True)
    monkeypatch.setenv("FMP_API_KEY", "test")
    monkeypatch.setattr(fmp, "_fetch", fake_fetch)

    out = fmp.fetch_fundamentals("AAPL", cache=Cache(LocalStore(tmp_path)))
    assert len(out) == 1
    assert out[0].gross_margin == pytest.approx(0.4)
    assert out[0].total_debt == 200
    assert out[0].operating_cash_flow == 120
    assert out[0].available_at == datetime(2025, 5, 1, 16, 5, tzinfo=timezone.utc)


def test_fmp_missing_balance_sheet_keeps_income_data(monkeypatch, tmp_path):
    """A gap in one statement must not drop the quarter entirely."""

    def fake_fetch(endpoint, asset, limit):
        if endpoint == "income-statement":
            return [{"date": "2025-03-31", "period": "Q1", "calendarYear": "2025", "revenue": 1000}]
        return []

    monkeypatch.setattr(fmp, "has_key", lambda: True)
    monkeypatch.setenv("FMP_API_KEY", "test")
    monkeypatch.setattr(fmp, "_fetch", fake_fetch)

    out = fmp.fetch_fundamentals("AAPL", cache=Cache(LocalStore(tmp_path)))
    assert len(out) == 1
    assert out[0].revenue == 1000
    assert out[0].total_debt is None  # missing stays missing


# ---------------------------------------------------------------------------
# NSE scraper — the failure paths matter most
# ---------------------------------------------------------------------------


def test_nse_parses_announcements():
    payload = [
        {
            "symbol": "RELIANCE",
            "desc": "Board approves buyback",
            "an_dt": "01-Jun-2025 10:30:00",
            "attchmntFile": "https://example.com/f.pdf",
        }
    ]
    items = nse_disclosures.parse_announcements(payload)
    assert len(items) == 1
    assert "RELIANCE" in items[0].headline
    assert "RELIANCE.NS" in items[0].asset_tags


def test_nse_announcements_wrong_shape_is_loud_and_empty(capsys):
    """The scenario this module exists for: NSE returns a dict instead of a
    list, and we must say so rather than return a confident empty."""
    assert nse_disclosures.parse_announcements({"unexpected": "shape"}) == []
    assert "CONTRACT BROKEN" in capsys.readouterr().err


def test_nse_announcements_renamed_fields_are_loud(capsys):
    """Rows arrive, but every field was renamed. Empty output here is a broken
    parser, not a quiet market — and it has to say so."""
    payload = [{"totally": "different", "field": "names"} for _ in range(5)]
    assert nse_disclosures.parse_announcements(payload) == []
    assert "CONTRACT BROKEN" in capsys.readouterr().err


def test_nse_truly_empty_payload_is_quiet(capsys):
    """No announcements today is legitimately empty and must NOT cry wolf."""
    assert nse_disclosures.parse_announcements([]) == []
    assert "CONTRACT BROKEN" not in capsys.readouterr().err


def test_nse_accepts_alternate_field_names():
    """NSE has shipped several field spellings; the parser knows the ones we
    have seen."""
    payload = [
        {"sm_name": "TCS", "subject": "Dividend declared", "sort_date": "2025-06-01 09:00:00"}
    ]
    assert len(nse_disclosures.parse_announcements(payload)) == 1


def test_nse_parses_fii_dii_flows():
    payload = [
        {"category": "FII/FPI", "date": "01-Jun-2025", "netValue": "-1,234.56"},
        {"category": "DII", "date": "01-Jun-2025", "netValue": "987.65"},
    ]
    obs = nse_disclosures.parse_fii_dii(payload)
    assert {o.series_id for o in obs} == {"FII_NET", "DII_NET"}
    assert obs[0].value == pytest.approx(-1234.56)


def test_nse_fii_dii_wrong_shape_is_loud(capsys):
    assert nse_disclosures.parse_fii_dii("not a list") == []
    assert "CONTRACT BROKEN" in capsys.readouterr().err


def test_nse_fii_dii_unparseable_rows_are_loud(capsys):
    assert nse_disclosures.parse_fii_dii([{"nope": 1}, {"nope": 2}]) == []
    assert "CONTRACT BROKEN" in capsys.readouterr().err


def test_nse_fetch_returns_empty_when_session_fails(monkeypatch):
    monkeypatch.setattr(nse_disclosures, "_session_get", lambda *a, **kw: None)
    assert nse_disclosures.fetch_announcements() == []
    assert nse_disclosures.fetch_fii_dii() == []
