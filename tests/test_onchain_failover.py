"""Tests for the on-chain failover that the CI outage exposed.

Background, because these tests only make sense with it: `binance_futures`
answers **HTTP 451** to every request from a datacenter IP. The scheduled scan
runs on a GitHub Actions runner, so for a month it fetched zero futures
positioning data while the workflow reported success — because health was
recorded per *kind* (`onchain`) and CoinGecko's single dominance reading kept the
aggregate at "1 item, ok".

Four things are pinned here, each mapping to one part of that failure:

1. Bybit parses correctly and reports errors that arrive with HTTP 200.
2. The failover triggers, and latches instead of re-probing per asset.
3. Two sources never blend into one series (the units differ).
4. Health is recorded per feed, so a dead feed cannot hide behind a live one.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from alpha_engine import health
from alpha_engine.analyzers.crypto_onchain import analyze_onchain
from alpha_engine.cache.interface import Cache, LocalStore
from alpha_engine.cache.models import OnChainObservation
from alpha_engine.ingestion import binance_futures, bybit_futures, gate_futures
from alpha_engine.orchestrator import engine

T0 = datetime(2026, 7, 1, tzinfo=timezone.utc)


class FakeResp:
    def __init__(self, payload, status_code=200):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


def _ok(rows):
    return FakeResp({"retCode": 0, "retMsg": "OK", "result": {"list": rows}})


# --------------------------------------------------------------------------
# 1. The Bybit adapter
# --------------------------------------------------------------------------


def test_bybit_parses_funding_rows(monkeypatch, tmp_path):
    rows = [
        {
            "symbol": "BTCUSDT",
            "fundingRate": "-0.00001212",
            "fundingRateTimestamp": "1785024000000",
        },
        {"symbol": "BTCUSDT", "fundingRate": "0.00004668", "fundingRateTimestamp": "1784995200000"},
    ]
    monkeypatch.setattr(bybit_futures.net, "get", lambda *a, **kw: _ok(rows))
    obs = bybit_futures.fetch_funding_rate("BTC", cache=Cache(LocalStore(tmp_path)))
    assert len(obs) == 2
    assert obs[0].metric == "funding_rate_BTC"
    assert obs[0].value == pytest.approx(-0.00001212)
    assert obs[0].source == "bybit_futures"


def test_bybit_open_interest_is_base_coin(monkeypatch, tmp_path):
    """Must match binance_futures' unit — see test_onchain.py for the other half."""
    rows = [{"openInterest": "57875.553", "timestamp": "1785024000000"}]
    monkeypatch.setattr(bybit_futures.net, "get", lambda *a, **kw: _ok(rows))
    obs = bybit_futures.fetch_open_interest("BTC", cache=Cache(LocalStore(tmp_path)))
    assert obs[0].value == pytest.approx(57875.553)


def test_bybit_reports_an_application_error_sent_with_http_200(monkeypatch, tmp_path):
    """Bybit signals 'invalid symbol' as retCode!=0 with HTTP 200. Trusting the
    status alone would cache an empty series as though the market went quiet."""
    monkeypatch.setattr(
        bybit_futures.net,
        "get",
        lambda *a, **kw: FakeResp({"retCode": 10001, "retMsg": "params error", "result": {}}),
    )
    assert bybit_futures.fetch_funding_rate("BTC", cache=Cache(LocalStore(tmp_path))) == []


def test_bybit_survives_a_malformed_body(monkeypatch, tmp_path):
    monkeypatch.setattr(
        bybit_futures.net, "get", lambda *a, **kw: FakeResp(["not", "an", "object"])
    )
    assert bybit_futures.fetch_open_interest("BTC", cache=Cache(LocalStore(tmp_path))) == []


def test_bybit_skips_malformed_rows_without_losing_the_good_ones(monkeypatch, tmp_path):
    rows = [
        {"fundingRate": "0.0001", "fundingRateTimestamp": "1785024000000"},
        {"fundingRate": None, "fundingRateTimestamp": "garbage"},
    ]
    monkeypatch.setattr(bybit_futures.net, "get", lambda *a, **kw: _ok(rows))
    assert len(bybit_futures.fetch_funding_rate("BTC", cache=Cache(LocalStore(tmp_path)))) == 1


def test_bybit_and_binance_cover_the_same_assets():
    """The fallback is useless if it supports fewer symbols than the primary."""
    for asset in ("BTC", "ETH", "SOL"):
        assert binance_futures.supports(asset)
        assert bybit_futures.supports(asset)
    assert not bybit_futures.supports("DOGE")


def test_bybit_http_error_returns_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(bybit_futures.net, "get", lambda *a, **kw: FakeResp({}, status_code=503))
    assert bybit_futures.fetch_funding_rate("BTC", cache=Cache(LocalStore(tmp_path))) == []


# --------------------------------------------------------------------------
# 1b. The Gate.io adapter — the tier that answers from BOTH environments
# --------------------------------------------------------------------------


def test_gate_parses_funding_rows(monkeypatch, tmp_path):
    """Gate returns a bare array with SECOND-resolution timestamps, unlike the
    millisecond ones Binance and Bybit use."""
    rows = [{"r": "0.000003", "t": 1785024001}, {"r": "-0.00003", "t": 1784995201}]
    monkeypatch.setattr(gate_futures.net, "get", lambda *a, **kw: FakeResp(rows))
    obs = gate_futures.fetch_funding_rate("BTC", cache=Cache(LocalStore(tmp_path)))

    assert len(obs) == 2
    assert obs[0].value == pytest.approx(0.000003)
    assert obs[0].source == "gate_futures"
    # Seconds read as seconds: 1785024001 is 2026, not 1970.
    assert obs[0].ts.year == 2026


def test_gate_seconds_are_not_read_as_milliseconds(monkeypatch, tmp_path):
    """The specific bug this guards: dividing by 1000 dates every observation to
    1970, the retention window drops them all, and the analyzer silently sees an
    empty series while the fetch reports success."""
    monkeypatch.setattr(
        gate_futures.net, "get", lambda *a, **kw: FakeResp([{"r": "0.0001", "t": 1785024001}])
    )
    obs = gate_futures.fetch_funding_rate("ETH", cache=Cache(LocalStore(tmp_path)))
    assert obs[0].ts > datetime(2020, 1, 1, tzinfo=timezone.utc)


def test_gate_open_interest_reads_the_contract_count(monkeypatch, tmp_path):
    rows = [{"time": 1785034500, "open_interest": 657343040, "open_interest_usd": 4239465572.8}]
    monkeypatch.setattr(gate_futures.net, "get", lambda *a, **kw: FakeResp(rows))
    obs = gate_futures.fetch_open_interest("BTC", cache=Cache(LocalStore(tmp_path)))
    assert obs[0].value == pytest.approx(657343040)


def test_gate_error_object_instead_of_a_list_is_refused(monkeypatch, tmp_path):
    """Gate answers errors with an object, sometimes at HTTP 200."""
    monkeypatch.setattr(
        gate_futures.net,
        "get",
        lambda *a, **kw: FakeResp({"label": "CONTRACT_NOT_FOUND", "message": "nope"}),
    )
    assert gate_futures.fetch_funding_rate("BTC", cache=Cache(LocalStore(tmp_path))) == []


def test_gate_http_error_returns_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(gate_futures.net, "get", lambda *a, **kw: FakeResp([], status_code=502))
    assert gate_futures.fetch_open_interest("BTC", cache=Cache(LocalStore(tmp_path))) == []


def test_gate_skips_malformed_rows(monkeypatch, tmp_path):
    rows = [{"r": "0.0001", "t": 1785024001}, {"r": None, "t": "garbage"}, {}]
    monkeypatch.setattr(gate_futures.net, "get", lambda *a, **kw: FakeResp(rows))
    assert len(gate_futures.fetch_funding_rate("BTC", cache=Cache(LocalStore(tmp_path)))) == 1


def test_every_chain_adapter_covers_the_same_three_assets():
    """A tier that supports fewer symbols silently narrows coverage when it wins."""
    for adapter in engine.FUTURES_CHAIN:
        for asset in ("BTC", "ETH", "SOL"):
            assert adapter.supports(asset), f"{adapter.SOURCE} is missing {asset}"
        assert not adapter.supports("DOGE")


# --------------------------------------------------------------------------
# 2. Failover behaviour
# --------------------------------------------------------------------------


def _refresh_onchain(monkeypatch, tmp_path, answers: dict[str, list]):
    """Run the onchain leg of refresh_context with the WHOLE chain stubbed.

    `answers` maps a source name to what that adapter returns. Every adapter in
    `FUTURES_CHAIN` is stubbed, not just the ones a given test cares about — an
    unstubbed adapter would reach the real internet, which is how this file
    first went red after a third tier was added.
    """
    calls: dict[str, list[str]] = {adapter.SOURCE: [] for adapter in engine.FUTURES_CHAIN}

    for adapter in engine.FUTURES_CHAIN:
        source = adapter.SOURCE

        def fake(asset, cache=None, _source=source):
            calls[_source].append(asset)
            return answers.get(_source, [])

        monkeypatch.setattr(adapter, "fetch_all", fake)

    monkeypatch.setattr(
        "alpha_engine.ingestion.coingecko.fetch_btc_dominance", lambda cache=None: 54.2
    )

    report = engine.refresh_context(
        Cache(LocalStore(tmp_path)), ("BTC", "ETH", "SOL"), kinds={"onchain"}
    )
    return report, calls


def _obs_from(source: str) -> list[OnChainObservation]:
    return [OnChainObservation(metric="funding_rate_BTC", ts=T0, value=0.0001, source=source)]


def test_the_chain_is_ordered_and_every_member_has_the_required_surface():
    """A chain member missing `SOURCE` or `supports` fails at runtime inside a
    scheduled job, which is the worst place to find out."""
    assert [a.SOURCE for a in engine.FUTURES_CHAIN] == [
        "binance_futures",
        "gate_futures",
        "bybit_futures",
    ]
    for adapter in engine.FUTURES_CHAIN:
        assert callable(adapter.supports)
        assert callable(adapter.fetch_all)
        assert adapter.supports("BTC")


def test_the_first_working_adapter_wins_and_the_rest_are_never_called(monkeypatch, tmp_path):
    first = engine.FUTURES_CHAIN[0]
    report, calls = _refresh_onchain(monkeypatch, tmp_path, {first.SOURCE: _obs_from("binance")})

    assert calls[first.SOURCE] == ["BTC", "ETH", "SOL"]
    for later in engine.FUTURES_CHAIN[1:]:
        assert calls[later.SOURCE] == [], f"{later.SOURCE} should not have been tried"
        # An untried fallback must not be recorded, or it ages into a false alarm.
        assert f"onchain.{later.SOURCE}" not in report.item_counts
    assert report.item_counts[f"onchain.{first.SOURCE}"] == 3


def test_a_blocked_primary_falls_through_to_the_next_working_tier(monkeypatch, tmp_path):
    """The real CI case: Binance answers 451 and Bybit answers 403, so the
    middle tier is the one that has to carry the run."""
    working = engine.FUTURES_CHAIN[1]
    report, calls = _refresh_onchain(monkeypatch, tmp_path, {working.SOURCE: _obs_from("gate")})

    assert calls[working.SOURCE] == ["BTC", "ETH", "SOL"]
    assert report.item_counts[f"onchain.{working.SOURCE}"] == 3
    assert report.item_counts[f"onchain.{engine.FUTURES_CHAIN[0].SOURCE}"] == 0


def test_the_choice_latches_instead_of_reprobing_every_asset(monkeypatch, tmp_path):
    """A geo-block is a property of the host, not the symbol. Re-probing would
    spend one doomed request per asset per run to relearn the same fact."""
    working = engine.FUTURES_CHAIN[-1]
    _report, calls = _refresh_onchain(monkeypatch, tmp_path, {working.SOURCE: _obs_from("bybit")})

    for dead in engine.FUTURES_CHAIN[:-1]:
        assert calls[dead.SOURCE] == ["BTC"], f"{dead.SOURCE} should be probed once, then dropped"
    assert calls[working.SOURCE] == ["BTC", "ETH", "SOL"]


def test_every_tier_blocked_reports_zero_everywhere(monkeypatch, tmp_path):
    """Nothing left to fall back to. Each tier must still be recorded at zero so
    `ingest --strict` can turn the run red."""
    report, _calls = _refresh_onchain(monkeypatch, tmp_path, {})
    for adapter in engine.FUTURES_CHAIN:
        assert report.item_counts[f"onchain.{adapter.SOURCE}"] == 0


# --------------------------------------------------------------------------
# 3. Two sources must never blend into one series
# --------------------------------------------------------------------------


def _oi(source: str, values: list[float], start: datetime) -> list[OnChainObservation]:
    return [
        OnChainObservation(
            metric="open_interest_BTC", ts=start + timedelta(days=i), value=v, source=source
        )
        for i, v in enumerate(values)
    ]


def _funding(value: float = 0.0001) -> list[OnChainObservation]:
    """Open interest only ever *confirms* a vote, so it is invisible without one.
    This supplies the vote so the OI note is actually rendered."""
    return [
        OnChainObservation(metric="funding_rate_BTC", ts=T0, value=value, source="bybit_futures")
    ]


def test_analyzer_uses_only_the_freshest_source_for_a_metric():
    """Binance historically wrote USD notional, Bybit writes base coin. Blended,
    the switchover looks like a 100,000x open-interest build-up."""
    old = _oi("binance_futures", [6_900_000_000.0, 6_950_000_000.0], T0)
    new = _oi("bybit_futures", [57_875.0, 57_900.0], T0 + timedelta(days=2))

    source = analyze_onchain(_funding() + old + new, asset="BTC")
    # Read off the fresh source alone: 57900/57875 - 1 ≈ +0.0%, not +100000%.
    assert "oi=+0.0%" in source.detail


def test_blending_two_units_would_have_faked_a_buildup():
    """Guard the guard: prove the blended series really is catastrophic, so the
    test above is testing something real rather than passing vacuously."""
    old = _oi("binance_futures", [6_900_000_000.0], T0)
    new = _oi("bybit_futures", [57_875.0], T0 + timedelta(days=1))
    blended = sorted(old + new, key=lambda o: o.ts)
    values = [o.value for o in blended]
    assert values[-1] / values[0] - 1.0 < -0.99  # a fabricated ~100% collapse


def test_a_single_source_series_is_untouched():
    only = _oi("bybit_futures", [100.0, 120.0], T0)
    source = analyze_onchain(_funding() + only, asset="BTC")
    assert "oi=+20.0%" in source.detail


def test_no_positioning_data_still_returns_a_zero_weight_neutral():
    source = analyze_onchain([], asset="BTC")
    assert source.weight == 0.0
    assert source.detail == "no positioning data"


# --------------------------------------------------------------------------
# 4. Per-feed health
# --------------------------------------------------------------------------


def test_health_is_recorded_per_feed_not_only_per_kind(monkeypatch, tmp_path):
    """The bug that hid the outage: one live feed kept the aggregate green."""
    health_file = tmp_path / "health.json"
    monkeypatch.setattr(health, "DEFAULT_PATH", health_file)

    working = engine.FUTURES_CHAIN[1]
    _refresh_onchain(monkeypatch, tmp_path, {working.SOURCE: _obs_from("gate")})

    recorded = health.load_health(health_file).sources
    assert "onchain" in recorded, "the aggregate is still recorded"
    assert "onchain.binance_futures" in recorded, "the dead feed must be visible on its own"
    assert "onchain.gate_futures" in recorded
    assert "onchain.coingecko_dominance" in recorded
    # The dead feed reports zero even though the kind's total is healthy.
    assert recorded["onchain.binance_futures"].total_items == 0
    assert recorded["onchain"].total_items > 0


def test_a_fetcher_returning_a_plain_int_still_records_its_kind(monkeypatch, tmp_path):
    """News records its own per-feed health inside rss.py, so it returns a total
    and must keep working."""
    health_file = tmp_path / "health.json"
    monkeypatch.setattr(health, "DEFAULT_PATH", health_file)
    monkeypatch.setattr("alpha_engine.ingestion.rss.fetch_all", lambda cache=None: [])
    monkeypatch.setattr("alpha_engine.ingestion.finnhub_news.has_key", lambda: False)

    report = engine.refresh_context(Cache(LocalStore(tmp_path)), ("BTC",), kinds={"news"})
    assert report.item_counts["news"] == 0
    assert "news" in health.load_health(health_file).sources
