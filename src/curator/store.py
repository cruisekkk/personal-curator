"""The "Keep" stage: one SQLite file plus plain-text bodies on disk.

Layout under the data dir (``$CURATOR_HOME``, default ``~/.curator``)::

    curator.db                      sources + items (metadata only)
    raw/YYYY-MM-DD/<item id>.txt    body text, by UTC publication date

Bodies live in files rather than in the database so that whoever does the
interpreting (a person, ``grep``, or an LLM with a Read tool) can open
them directly. Dedupe is a ``UNIQUE(source_id, external_id)`` constraint:
re-running a fetch is always safe.
"""
from __future__ import annotations

import html
import json
import os
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from curator.ingest.base import RawItem

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id              INTEGER PRIMARY KEY,
    kind            TEXT NOT NULL,
    name            TEXT NOT NULL UNIQUE,
    config          TEXT NOT NULL DEFAULT '{}',
    last_fetched_at TEXT
);
CREATE TABLE IF NOT EXISTS items (
    id           INTEGER PRIMARY KEY,
    source_id    INTEGER NOT NULL REFERENCES sources(id),
    external_id  TEXT NOT NULL,
    title        TEXT NOT NULL DEFAULT '',
    url          TEXT NOT NULL,
    published_at TEXT NOT NULL,
    lang         TEXT,
    metadata     TEXT NOT NULL DEFAULT '{}',
    raw_path     TEXT NOT NULL,
    fetched_at   TEXT NOT NULL,
    UNIQUE (source_id, external_id)
);
CREATE INDEX IF NOT EXISTS idx_items_published ON items (published_at);
"""

_TAG_RE = re.compile(r"<[^>]+>")
_PARA_END_RE = re.compile(r"</(p|div|h[1-6])>", re.IGNORECASE)
_LINE_END_RE = re.compile(r"</(li|tr)>|<br\s*/?>", re.IGNORECASE)
_BLANKS_RE = re.compile(r"\n\s*\n+")


def default_home() -> Path:
    return Path(os.environ.get("CURATOR_HOME") or Path.home() / ".curator").expanduser()


def to_text(body: str) -> str:
    """Cheap HTML -> text: enough for clustering and reading, not a full extractor."""
    text = _LINE_END_RE.sub("\n", _PARA_END_RE.sub("\n\n", body))
    text = html.unescape(_TAG_RE.sub("", text))
    return _BLANKS_RE.sub("\n\n", text).strip()


def _iso(dt: datetime) -> str:
    """UTC ISO-8601 with a fixed width, so string comparison in SQL is chronological."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class Store:
    def __init__(self, home: Path | None = None) -> None:
        self.home = Path(home) if home else default_home()
        self.home.mkdir(parents=True, exist_ok=True)
        self.raw_dir = self.home / "raw"
        self.conn = sqlite3.connect(self.home / "curator.db")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def upsert_source(self, kind: str, name: str, config: dict | None = None) -> int:
        """Return the id of the source called ``name``, creating it if needed."""
        cfg = json.dumps(config or {}, sort_keys=True)
        self.conn.execute(
            "INSERT INTO sources (kind, name, config) VALUES (?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET kind = excluded.kind, config = excluded.config",
            (kind, name, cfg),
        )
        self.conn.commit()
        row = self.conn.execute("SELECT id FROM sources WHERE name = ?", (name,)).fetchone()
        return int(row[0])

    def record_item(self, source_id: int, item: RawItem) -> int | None:
        """Insert ``item``; return its id, or None if it was already stored."""
        published = _iso(item.published_at)
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO items "
            "(source_id, external_id, title, url, published_at, lang, metadata, raw_path, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, '', ?)",
            (
                source_id,
                item.external_id,
                item.title,
                item.url,
                published,
                item.lang,
                json.dumps(item.metadata, sort_keys=True, default=str),
                _iso(datetime.now(UTC)),
            ),
        )
        if cur.rowcount == 0:
            return None
        item_id = int(cur.lastrowid)
        # The file name needs the row id, so write the file after the insert
        # and commit only once both exist.
        rel = Path("raw") / published[:10] / f"{item_id}.txt"
        path = self.home / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(to_text(item.body) + "\n", encoding="utf-8")
        self.conn.execute("UPDATE items SET raw_path = ? WHERE id = ?", (str(rel), item_id))
        self.conn.commit()
        return item_id

    def last_fetched_at(self, source_id: int) -> datetime | None:
        row = self.conn.execute(
            "SELECT last_fetched_at FROM sources WHERE id = ?", (source_id,)
        ).fetchone()
        if not row or not row[0]:
            return None
        return datetime.strptime(row[0], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)

    def mark_fetched(self, source_id: int, when: datetime) -> None:
        self.conn.execute(
            "UPDATE sources SET last_fetched_at = ? WHERE id = ?", (_iso(when), source_id)
        )
        self.conn.commit()

    def count_items(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM items").fetchone()[0])
