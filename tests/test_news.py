"""Tests for Phase 11a: news ingestion (RSS/Atom, Finnhub) and the
deterministic sentiment analyzer.

The sentiment tests matter more than the parsing ones. Sentiment produces a
*weight*, which makes it decision-bearing, which puts it squarely under the
cardinal rule: same headline, same score, forever. If these tests ever need a
tolerance for non-determinism, something has gone badly wrong.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from alpha_engine.analyzers.sentiment import (
    MAX_WEIGHT,
    analyze_sentiment,
    score_headline,
    score_news,
)
from alpha_engine.cache.models import NewsItem
from alpha_engine.ingestion import finnhub_news, rss
from alpha_engine.schema.signal import Direction

NOW = datetime(2025, 6, 1, tzinfo=timezone.utc)


def _item(headline: str, days_ago: float = 0.0, tags: list[str] | None = None) -> NewsItem:
    return NewsItem(
        ts=NOW - timedelta(days=days_ago),
        headline=headline,
        source="test",
        asset_tags=tags or [],
    )


# ---------------------------------------------------------------------------
# RSS / Atom parsing
# ---------------------------------------------------------------------------

RSS_SAMPLE = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <item>
    <title>Company beats earnings expectations</title>
    <link>https://example.com/a</link>
    <pubDate>Mon, 26 May 2025 10:00:00 GMT</pubDate>
  </item>
  <item>
    <title>Apple announces record buyback</title>
    <link>https://example.com/b</link>
    <pubDate>Tue, 27 May 2025 10:00:00 GMT</pubDate>
  </item>
</channel></rss>"""

ATOM_SAMPLE = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>Bitcoin ETF approval granted</title>
    <link href="https://example.com/c"/>
    <updated>2025-05-28T12:00:00Z</updated>
  </entry>
</feed>"""


def test_parses_rss_items():
    items = rss.parse_feed(RSS_SAMPLE, "test_feed")
    assert len(items) == 2
    assert items[0].headline == "Company beats earnings expectations"
    assert items[0].url == "https://example.com/a"
    assert items[0].source == "test_feed"
    assert items[0].ts.year == 2025


def test_parses_atom_entries():
    items = rss.parse_feed(ATOM_SAMPLE, "atom_feed")
    assert len(items) == 1
    assert items[0].headline == "Bitcoin ETF approval granted"
    assert items[0].url == "https://example.com/c"


def test_tags_assets_from_headline():
    items = rss.parse_feed(RSS_SAMPLE, "test_feed")
    assert "AAPL" in items[1].asset_tags


def test_tagging_respects_word_boundaries():
    """A substring match would tag half the internet. 'ethics' is not ETH."""
    assert rss.tag_assets("New ethics guidelines published") == []
    assert "ETH" in rss.tag_assets("Ethereum upgrade ships")


def test_malformed_xml_returns_empty_not_raises():
    """A changed or broken feed must degrade, never crash a scan."""
    assert rss.parse_feed("<not xml", "broken") == []
    assert rss.parse_feed("", "empty") == []


def test_feed_without_titles_is_skipped():
    xml = '<?xml version="1.0"?><rss><channel><item><link>x</link></item></channel></rss>'
    assert rss.parse_feed(xml, "s") == []


def test_fetch_feed_rejects_unknown_source():
    import pytest

    with pytest.raises(ValueError, match="unknown feed"):
        rss.fetch_feed("not_a_real_feed")


def test_fetch_feed_survives_network_failure(monkeypatch, tmp_path):
    """The whole point of the try/except: a dead feed costs you the feed, not
    the run."""
    from alpha_engine.cache.interface import Cache, LocalStore

    def boom(*a, **kw):
        raise OSError("network down")

    monkeypatch.setattr(rss.net, "get", boom)
    assert rss.fetch_feed("fed_press", cache=Cache(LocalStore(tmp_path))) == []


# ---------------------------------------------------------------------------
# Sentiment scoring — the decision-bearing part
# ---------------------------------------------------------------------------


def test_positive_headline_scores_positive():
    assert score_headline("Profit surges to record high") > 0


def test_negative_headline_scores_negative():
    assert score_headline("Shares plunge on fraud investigation") < 0


def test_headline_without_lexicon_terms_is_none():
    """None means 'no opinion', which is different from a neutral 0.0 opinion."""
    assert score_headline("Company schedules annual general meeting") is None


def test_empty_headline_is_none():
    assert score_headline("") is None
    assert score_headline("   ") is None


def test_negation_flips_the_sign():
    positive = score_headline("Quarter was profitable")
    negated = score_headline("Quarter was not profitable")
    assert positive is not None and negated is not None
    assert positive > 0 > negated


def test_negation_only_applies_within_its_window():
    """'not' four words back should not reach the term; otherwise a single
    negation anywhere would invert an entire headline."""
    assert score_headline("not a b c d profit") > 0


def test_score_is_bounded():
    loud = score_headline("surge soar rally beat upgrade record profit growth gains")
    assert -1.0 <= loud <= 1.0


def test_scoring_is_deterministic():
    """The cardinal rule, tested directly."""
    text = "Earnings beat but margins decline on weak demand"
    assert score_headline(text) == score_headline(text)


def test_score_news_attaches_scores_without_mutating():
    items = [_item("Profit surges")]
    scored = score_news(items)
    assert scored[0].sentiment_score is not None
    assert items[0].sentiment_score is None  # original untouched


# ---------------------------------------------------------------------------
# The analyzer
# ---------------------------------------------------------------------------


def test_no_news_yields_zero_weight():
    src = analyze_sentiment([], now=NOW)
    assert src.direction is Direction.NEUTRAL
    assert src.weight == 0.0


def test_unscorable_news_yields_zero_weight():
    src = analyze_sentiment([_item("Board meeting scheduled")], now=NOW)
    assert src.weight == 0.0


def test_bullish_news_produces_bullish_source():
    items = [_item("Profit surges to record"), _item("Analysts upgrade the stock")]
    src = analyze_sentiment(items, now=NOW)
    assert src.direction is Direction.BULLISH
    assert src.weight > 0


def test_bearish_news_produces_bearish_source():
    items = [_item("Shares plunge on fraud probe"), _item("Guidance cut, layoffs announced")]
    src = analyze_sentiment(items, now=NOW)
    assert src.direction is Direction.BEARISH


def test_mixed_news_lands_neutral():
    items = [_item("Profit surges"), _item("Shares plunge")]
    src = analyze_sentiment(items, now=NOW)
    assert src.direction is Direction.NEUTRAL


def test_weight_never_exceeds_the_cap():
    """News is a reason to look, not a reason to act."""
    items = [_item("Profit surges to record on strong growth and buyback") for _ in range(50)]
    src = analyze_sentiment(items, now=NOW)
    assert src.weight <= MAX_WEIGHT


def test_stale_news_is_ignored_entirely():
    """A headline older than MAX_AGE_DAYS is history, not news."""
    src = analyze_sentiment([_item("Profit surges", days_ago=60)], now=NOW)
    assert src.weight == 0.0


def test_fresh_news_outweighs_old_news():
    fresh = analyze_sentiment([_item("Profit surges", days_ago=0)], now=NOW)
    old = analyze_sentiment([_item("Profit surges", days_ago=10)], now=NOW)
    assert fresh.weight > old.weight


def test_more_corroborating_headlines_raises_weight():
    one = analyze_sentiment([_item("Profit surges")], now=NOW)
    many = analyze_sentiment([_item("Profit surges") for _ in range(9)], now=NOW)
    assert many.weight > one.weight


def test_asset_filter_selects_tagged_headlines_only():
    items = [
        _item("Apple profit surges", tags=["AAPL"]),
        _item("Rival shares plunge on fraud", tags=["MSFT"]),
    ]
    src = analyze_sentiment(items, asset="AAPL", now=NOW)
    assert src.direction is Direction.BULLISH


def test_asset_filter_with_no_matches_is_zero_weight():
    items = [_item("Apple profit surges", tags=["AAPL"])]
    assert analyze_sentiment(items, asset="NVDA", now=NOW).weight == 0.0


def test_future_dated_headline_is_treated_as_fresh_not_negative():
    """Feeds do emit bad dates. A negative age must not produce a decay > 1."""
    src = analyze_sentiment([_item("Profit surges", days_ago=-5)], now=NOW)
    assert 0 < src.weight <= MAX_WEIGHT


def test_analyzer_is_deterministic():
    items = [_item("Profit surges"), _item("Weak guidance cut")]
    a = analyze_sentiment(items, now=NOW)
    b = analyze_sentiment(items, now=NOW)
    assert (a.direction, a.weight, a.detail) == (b.direction, b.weight, b.detail)


# ---------------------------------------------------------------------------
# Finnhub gating
# ---------------------------------------------------------------------------


def test_finnhub_without_key_returns_empty(monkeypatch):
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    monkeypatch.setattr(finnhub_news, "has_key", lambda: False)
    assert finnhub_news.fetch_company_news("AAPL") == []


def test_finnhub_parses_rows(monkeypatch, tmp_path):
    from alpha_engine.cache.interface import Cache, LocalStore

    class FakeResp:
        status_code = 200

        def json(self):
            return [
                {
                    "headline": "Apple profit surges",
                    "datetime": 1717200000,
                    "url": "https://example.com/x",
                }
            ]

    monkeypatch.setattr(finnhub_news, "has_key", lambda: True)
    monkeypatch.setenv("FINNHUB_API_KEY", "test")
    monkeypatch.setattr(finnhub_news.net, "get", lambda *a, **kw: FakeResp())

    items = finnhub_news.fetch_company_news("AAPL", cache=Cache(LocalStore(tmp_path)))
    assert len(items) == 1
    assert "AAPL" in items[0].asset_tags


def test_finnhub_http_error_returns_empty(monkeypatch):
    class FakeResp:
        status_code = 429

        def json(self):
            return {}

    monkeypatch.setattr(finnhub_news, "has_key", lambda: True)
    monkeypatch.setenv("FINNHUB_API_KEY", "test")
    monkeypatch.setattr(finnhub_news.net, "get", lambda *a, **kw: FakeResp())
    assert finnhub_news.fetch_company_news("AAPL") == []


def test_finnhub_wrong_response_shape_fails_loudly(monkeypatch, capsys):
    class FakeResp:
        status_code = 200

        def json(self):
            return {"error": "changed shape"}

    monkeypatch.setattr(finnhub_news, "has_key", lambda: True)
    monkeypatch.setenv("FINNHUB_API_KEY", "test")
    monkeypatch.setattr(finnhub_news.net, "get", lambda *a, **kw: FakeResp())
    assert finnhub_news.fetch_company_news("AAPL", store=False) == []
    assert "CONTRACT BROKEN" in capsys.readouterr().err


def test_finnhub_history_export_preserves_existing_archive(monkeypatch, tmp_path):
    import json
    import sys

    from alpha_engine.cache.models import NewsItem

    item = NewsItem(
        ts=datetime(2026, 1, 1, tzinfo=timezone.utc),
        headline="Apple profit rises",
        source="finnhub",
        asset_tags=["AAPL"],
    )
    monkeypatch.setattr(finnhub_news, "has_key", lambda: True)
    monkeypatch.setattr(finnhub_news, "fetch_company_news", lambda *a, **kw: [item])
    output = tmp_path / "history.json"
    monkeypatch.setattr(sys, "argv", ["finnhub_news", "AAPL", "--output", str(output)])

    assert finnhub_news.main() == 0
    before = output.read_text()
    saved = json.loads(before)
    assert saved["asset"] == "AAPL"
    assert saved["provider"] == "finnhub"
    assert saved["items"][0]["headline"] == "Apple profit rises"
    assert (
        date.fromisoformat(saved["requested_to"]) - date.fromisoformat(saved["requested_from"])
    ).days == 365
    with pytest.raises(SystemExit):
        finnhub_news.main()
    assert output.read_text() == before
