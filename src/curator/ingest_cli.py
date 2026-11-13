"""``curator-ingest``: fetch RSS/Atom feeds into the store.

    curator-ingest --feed https://example.com/feed.xml --since-last-fetch

Each feed is a row in ``sources`` (named by its URL). With
``--since-last-fetch`` only entries published after the previous
successful fetch are considered; without it every entry in the feed is
offered to the store and dedupe drops the ones already kept.
"""
from __future__ import annotations

import argparse
import sys
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from curator.ingest.base import FetchError, IngestSource, RawItem
from curator.ingest.rss import RssIngestor
from curator.store import Store


def ingest_feed(
    store: Store,
    url: str,
    *,
    since_last_fetch: bool = False,
    lang: str | None = None,
    source: IngestSource | None = None,
) -> tuple[int, int]:
    """Fetch one feed; return ``(new, duplicates)``. Raises ``FetchError``.

    ``last_fetched_at`` moves only after the whole feed was consumed, and
    it moves to the time the fetch *started*: anything published while we
    were fetching is picked up next time instead of falling in the gap.
    """
    source = source or RssIngestor()
    config = {"url": url, **({"lang": lang} if lang else {})}
    source_id = store.upsert_source(source.name, url, config)
    since = store.last_fetched_at(source_id) if since_last_fetch else None
    started = datetime.now(UTC)
    items: Iterable[RawItem] = source.fetch_new(config, since)
    new = dup = 0
    for item in items:
        if store.record_item(source_id, item) is None:
            dup += 1
        else:
            new += 1
    store.mark_fetched(source_id, started)
    return new, dup


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch RSS/Atom feeds into the curator store")
    parser.add_argument("--feed", action="append", required=True, metavar="URL",
                        help="feed URL (repeatable)")
    parser.add_argument("--since-last-fetch", action="store_true",
                        help="skip entries published before this feed's last successful fetch")
    parser.add_argument("--lang", default=None, help="ISO 639-1 language tag to record on items")
    parser.add_argument("--home", type=Path, default=None,
                        help="data dir (default: $CURATOR_HOME or ~/.curator)")
    args = parser.parse_args(argv)

    store = Store(args.home)
    failed = 0
    try:
        for url in args.feed:
            try:
                new, dup = ingest_feed(
                    store, url, since_last_fetch=args.since_last_fetch, lang=args.lang
                )
            except FetchError as exc:
                failed += 1
                print(f"error: {exc}", file=sys.stderr)
                continue
            # A high duplicate count is the healthy case: it means dedupe works.
            print(f"{url}: {new} new, {dup} duplicates")
    finally:
        store.close()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
