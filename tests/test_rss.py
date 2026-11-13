from datetime import UTC, datetime

import pytest

from curator.ingest.base import FetchError
from curator.ingest.rss import RssIngestor, parse_feed

FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Example Wire</title>
    <link>https://example.com/</link>
    <item>
      <title>Northwind raises guidance</title>
      <link>https://example.com/news/northwind</link>
      <guid>wire-001</guid>
      <pubDate>Mon, 02 Mar 2026 07:10:00 GMT</pubDate>
      <description>&lt;p&gt;Capex guidance up 30%.&lt;/p&gt;</description>
    </item>
    <item>
      <title>Halcyon cuts prices</title>
      <link>https://example.com/news/halcyon</link>
      <pubDate>Mon, 02 Mar 2026 11:05:00 GMT</pubDate>
      <description>Inference prices down 40%.</description>
    </item>
    <item>
      <title>No id and no link: skipped</title>
    </item>
  </channel>
</rss>
"""


def test_parse_feed_maps_entries_to_raw_items():
    items = list(parse_feed(FEED, feed_url="https://example.com/feed", lang="en"))
    assert [i.external_id for i in items] == ["wire-001", "https://example.com/news/halcyon"]

    first = items[0]
    assert first.title == "Northwind raises guidance"
    assert first.url == "https://example.com/news/northwind"
    assert first.published_at == datetime(2026, 3, 2, 7, 10, tzinfo=UTC)
    assert "Capex guidance up 30%" in first.body
    assert first.lang == "en"
    assert first.metadata == {"feed_url": "https://example.com/feed", "feed_title": "Example Wire"}


def test_parse_feed_filters_by_since():
    since = datetime(2026, 3, 2, 7, 10, tzinfo=UTC)  # equal to the first item: excluded
    items = list(parse_feed(FEED, since=since))
    assert [i.title for i in items] == ["Halcyon cuts prices"]


def test_non_http_url_is_rejected_without_touching_the_network():
    with pytest.raises(FetchError):
        list(RssIngestor().fetch_new({"url": "file:///etc/hosts"}, None))
    with pytest.raises(FetchError):
        list(RssIngestor().fetch_new({}, None))
