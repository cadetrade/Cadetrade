import json
from datetime import datetime, timezone

from cadetrade import extnews

NOW = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)
RSS = b"""<?xml version="1.0"?><rss><channel>
<item><title>Fed holds rates</title><pubDate>Wed, 11 Nov 2026 10:00:00 GMT</pubDate></item>
<item><title>Old story</title><pubDate>Mon, 02 Nov 2026 10:00:00 GMT</pubDate></item>
<item><title>FED HOLDS RATES</title><pubDate>Wed, 11 Nov 2026 09:00:00 GMT</pubDate></item>
</channel></rss>"""


def test_rss_recent_and_deduplicated(monkeypatch):
    monkeypatch.setattr(extnews, "_get", lambda url, ttl: RSS if "a" in url else b"not xml")
    out = extnews.headlines(NOW, feeds={"A": "http://a", "B": "http://b"})
    assert [n["titolo"] for n in out] == ["Fed holds rates"]


def test_calendar_usd_high_impact_window():
    rows = [{"title": "CPI m/m", "country": "USD", "date": "2026-11-11T08:30:00-05:00", "impact": "High",
             "forecast": "0.3%", "previous": "0.4%"},
            {"title": "GDP", "country": "EUR", "date": "2026-11-11T05:00:00-05:00", "impact": "High"},
            {"title": "Bank holiday", "country": "USD", "date": "2026-11-11T00:00:00-05:00", "impact": "Holiday"},
            {"title": "Retail Sales", "country": "USD", "date": "2026-11-20T08:30:00-05:00", "impact": "High"}]
    out = extnews.parse_calendar(json.dumps(rows).encode(), NOW)
    assert [e["evento"] for e in out] == ["CPI m/m"] and out[0]["impatto"] == "ALTO"
