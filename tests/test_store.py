from datetime import UTC, datetime, timedelta

from curator.ingest.base import FetchError, IngestSource, RawItem
from curator.ingest_cli import ingest_feed
from curator.store import Store, to_text


def _item(eid: str, published: datetime, body: str = "<p>Hello &amp; welcome</p>") -> RawItem:
    return RawItem(
        external_id=eid, title=f"Title {eid}", body=body,
        url=f"https://example.com/{eid}", published_at=published,
    )


class FakeSource(IngestSource):
    """Offline stand-in for a feed: honours ``since`` like the RSS collector."""

    name = "fake"

    def __init__(self, items: list[RawItem], fail: bool = False) -> None:
        self.items, self.fail, self.seen_since = items, fail, []

    def fetch_new(self, source_config, since):
        self.seen_since.append(since)
        if self.fail:
            raise FetchError("boom")
        for it in self.items:
            if since is None or it.published_at > since:
                yield it


def test_to_text_strips_tags_and_entities():
    assert to_text("<p>One &amp; two</p><p>Three<br>Four</p>") == "One & two\n\nThree\nFour"


def test_record_item_dedupes_and_writes_body(tmp_path):
    store = Store(tmp_path)
    sid = store.upsert_source("rss", "https://example.com/feed")
    when = datetime(2026, 3, 2, 23, 30, tzinfo=UTC)

    first = store.record_item(sid, _item("a", when))
    assert first is not None
    assert store.record_item(sid, _item("a", when)) is None  # UNIQUE(source_id, external_id)
    assert store.count_items() == 1

    raw = tmp_path / "raw" / "2026-03-02" / f"{first}.txt"
    assert raw.read_text(encoding="utf-8").strip() == "Hello & welcome"

    # The same external_id under another source is a different item.
    other = store.upsert_source("rss", "https://example.com/other")
    assert store.record_item(other, _item("a", when)) is not None
    assert store.upsert_source("rss", "https://example.com/feed") == sid
    store.close()


def test_since_last_fetch_bookkeeping(tmp_path):
    store = Store(tmp_path)
    url = "https://example.com/feed"
    old = datetime.now(UTC) - timedelta(days=2)
    src = FakeSource([_item("a", old), _item("b", old + timedelta(hours=1))])

    assert ingest_feed(store, url, since_last_fetch=True, source=src) == (2, 0)
    assert src.seen_since == [None]  # first run: nothing to compare against
    sid = store.upsert_source("fake", url)
    first_mark = store.last_fetched_at(sid)
    assert first_mark is not None

    # Second run passes the stored mark as `since`; only the new item comes through.
    src.items.append(_item("c", datetime.now(UTC) + timedelta(seconds=5)))
    assert ingest_feed(store, url, since_last_fetch=True, source=src) == (1, 0)
    assert src.seen_since[-1] == first_mark
    assert store.last_fetched_at(sid) >= first_mark

    # Without the flag everything is offered again and dedupe absorbs it.
    assert ingest_feed(store, url, source=src) == (0, 3)
    store.close()


def test_failed_fetch_does_not_advance_last_fetched(tmp_path):
    store = Store(tmp_path)
    url = "https://example.com/feed"
    sid = store.upsert_source("fake", url)
    mark = datetime(2026, 3, 1, tzinfo=UTC)
    store.mark_fetched(sid, mark)

    try:
        ingest_feed(store, url, since_last_fetch=True, source=FakeSource([], fail=True))
    except FetchError:
        pass
    else:
        raise AssertionError("expected FetchError")
    assert store.last_fetched_at(sid) == mark
    store.close()
