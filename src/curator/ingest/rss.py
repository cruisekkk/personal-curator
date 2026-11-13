"""Generic RSS / Atom collector.

Source config shape::

    {"url": "https://example.com/feed", "lang": "en"}   # lang is optional

``feedparser`` does the parsing because it handles the long tail of
malformed feeds (old Atom/RSS dialects, odd date formats, BOMs) that a
hand-rolled parser would not. The HTTP fetch uses the standard library so
we control the timeout and User-Agent without an extra dependency.
"""
from __future__ import annotations

import logging
import urllib.error
import urllib.request
from collections.abc import Iterator
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, ClassVar

import feedparser

from curator.ingest.base import FetchError, IngestSource, RawItem

logger = logging.getLogger(__name__)

# Some feeds reject generic client strings; an identifiable one is friendlier.
DEFAULT_USER_AGENT = "personal-curator/0.1 (RSS reader)"
# Most feeds answer in under 2s; 15s covers the slow tail.
DEFAULT_TIMEOUT_SECONDS = 15.0


def _parse_published(entry: dict[str, Any]) -> datetime | None:
    """Return a tz-aware datetime for an entry, or None if it has no usable date."""
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed is not None:
        # feedparser's parsed struct is UTC by convention.
        return datetime(*parsed[:6], tzinfo=UTC)
    raw = entry.get("published") or entry.get("updated")
    if isinstance(raw, str) and raw:
        try:
            dt = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    return None


def _pick_body(entry: dict[str, Any]) -> str:
    """Richest text available: Atom <content>, then summary/description, then title."""
    for c in entry.get("content") or []:
        if c.get("value"):
            return str(c["value"])
    if entry.get("summary"):
        return str(entry["summary"])
    return str(entry.get("title", ""))


def _pick_external_id(entry: dict[str, Any]) -> str | None:
    """Stable per-entry id. Prefer id/guid; fall back to the link."""
    eid = entry.get("id") or entry.get("guid") or entry.get("link")
    if isinstance(eid, str) and eid.strip():
        return eid.strip()
    return None


def parse_feed(
    content: bytes | str,
    *,
    feed_url: str = "",
    since: datetime | None = None,
    lang: str | None = None,
) -> Iterator[RawItem]:
    """Parse feed XML and yield its entries as ``RawItem``s.

    Entries published at or before ``since`` are skipped. Entries with no
    parseable date are always yielded; the store's dedupe keeps re-runs
    idempotent anyway.
    """
    parsed = feedparser.parse(content)
    if parsed.bozo and parsed.bozo_exception:
        # bozo means "irregular", not "fatal": feedparser salvages a lot.
        logger.info("feed %s is malformed: %s", feed_url, parsed.bozo_exception)
    feed_title = parsed.feed.get("title", "") if parsed.feed else ""
    for entry in parsed.entries:
        external_id = _pick_external_id(entry)
        if external_id is None:
            continue
        published = _parse_published(entry)
        if since is not None and published is not None and published <= since:
            continue
        yield RawItem(
            external_id=external_id,
            title=str(entry.get("title", "")),
            body=_pick_body(entry),
            url=str(entry.get("link", external_id)),
            published_at=published or datetime.now(UTC),
            lang=lang,
            metadata={"feed_url": feed_url, "feed_title": feed_title},
        )


class RssIngestor(IngestSource):
    """Fetch one RSS / Atom feed over HTTP(S) and yield its entries."""

    name: ClassVar[str] = "rss"

    def __init__(
        self,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._user_agent = user_agent
        self._timeout = timeout

    def _get_bytes(self, url: str) -> bytes:
        if not url.startswith(("http://", "https://")):
            raise FetchError(f"unsupported feed URL (need http/https): {url}")
        req = urllib.request.Request(url, headers={"User-Agent": self._user_agent})
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                return resp.read()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise FetchError(f"fetch failed for {url}: {exc}") from exc

    def fetch_new(self, source_config: dict[str, Any], since: datetime | None) -> Iterator[RawItem]:
        url = source_config.get("url")
        if not isinstance(url, str) or not url:
            raise FetchError("rss source config has no 'url'")
        yield from parse_feed(
            self._get_bytes(url), feed_url=url, since=since, lang=source_config.get("lang")
        )
