"""SQLite persistence layer for rss-mcp.

Two tables, one file, no ORM:

* ``feeds``  -- one row per subscription, plus the HTTP cache validators
  (``etag`` / ``last_modified``) needed for conditional requests.
* ``entries`` -- one row per feed item, deduplicated on ``(feed_id, guid)`` so a
  repeated refresh can never create duplicates.

A fresh connection is opened per unit of work. That keeps the module safe no
matter which thread/event loop the MCP framework dispatches a tool on, and it
avoids leaking long-lived sqlite handles into a stdio subprocess.
"""

from __future__ import annotations

import calendar
import sqlite3
import time
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS feeds (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    url             TEXT NOT NULL UNIQUE,
    title           TEXT,
    site_url        TEXT,
    description     TEXT,
    etag            TEXT,
    last_modified   TEXT,
    added_at        TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    last_fetched_at TEXT,
    last_error      TEXT,
    is_active       INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS entries (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    feed_id      INTEGER NOT NULL REFERENCES feeds(id) ON DELETE CASCADE,
    guid         TEXT NOT NULL,
    url          TEXT,
    title        TEXT,
    author       TEXT,
    summary      TEXT,
    content      TEXT,
    published_at TEXT,
    published_ts INTEGER,
    fetched_at   TEXT NOT NULL,
    is_read      INTEGER NOT NULL DEFAULT 0,
    is_starred   INTEGER NOT NULL DEFAULT 0,
    UNIQUE (feed_id, guid)
);

CREATE INDEX IF NOT EXISTS idx_entries_feed_published
    ON entries (feed_id, published_ts DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_entries_published
    ON entries (published_ts DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_entries_read
    ON entries (is_read, published_ts DESC);
CREATE INDEX IF NOT EXISTS idx_entries_starred
    ON entries (is_starred, published_ts DESC);
"""

_VIEWS = """
CREATE VIEW IF NOT EXISTS feed_overview AS
SELECT
    f.id, f.url, f.title, f.site_url, f.description,
    f.etag, f.last_modified, f.added_at, f.updated_at,
    f.last_fetched_at, f.last_error, f.is_active,
    (SELECT COUNT(*) FROM entries e WHERE e.feed_id = f.id) AS entry_count,
    (SELECT COUNT(*) FROM entries e WHERE e.feed_id = f.id AND e.is_read = 0)
        AS unread_count
FROM feeds f;

CREATE VIEW IF NOT EXISTS entry_overview AS
SELECT
    e.id, e.feed_id, e.guid, e.url, e.title, e.author, e.summary, e.content,
    e.published_at, e.published_ts, e.fetched_at, e.is_read, e.is_starred,
    f.title AS feed_title, f.url AS feed_url
FROM entries e
JOIN feeds f ON f.id = e.feed_id;
"""


def utc_now_iso() -> str:
    """Current UTC time as ``YYYY-MM-DDTHH:MM:SSZ``."""

    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_to_timestamp(value: str | None) -> int | None:
    """Parse a feed-supplied ISO-8601 timestamp into epoch seconds."""

    if not value:
        return None
    candidate = value.strip()
    if candidate.endswith(("Z", "z")):
        candidate = candidate[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp())


def struct_to_timestamp(struct: time.struct_time | None) -> int | None:
    """Turn a :mod:`feedparser` ``*_parsed`` struct into epoch seconds (UTC)."""

    if not struct:
        return None
    try:
        return int(calendar.timegm(struct))
    except (OverflowError, TypeError, ValueError):  # pragma: no cover - odd feeds
        return None


def timestamp_to_iso(timestamp: int | None) -> str | None:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(slots=True)
class NewEntry:
    """A parsed feed item, ready to be persisted."""

    guid: str
    url: str | None = None
    title: str | None = None
    author: str | None = None
    summary: str | None = None
    content: str | None = None
    published_ts: int | None = None


class Database:
    """Thin, explicit SQLite wrapper. All methods are synchronous and short-lived."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._initialised = False

    # ------------------------------------------------------------------ setup

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=15.0, isolation_level="DEFERRED")
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def initialize(self) -> None:
        """Create the schema (idempotent) and enable WAL once per process."""

        if self._initialised:
            return
        if self.path != ":memory:":
            Path(self.path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            if self.path != ":memory:":
                conn.execute("PRAGMA journal_mode = WAL")
                conn.execute("PRAGMA synchronous = NORMAL")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"database {self.path} uses schema v{version}, this build speaks "
                    f"v{SCHEMA_VERSION}; upgrade rss-mcp"
                )
            conn.executescript(_SCHEMA)
            conn.executescript(_VIEWS)
            if version < SCHEMA_VERSION:
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self._initialised = True

    # ------------------------------------------------------------------ feeds

    def get_feed(self, feed_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM feed_overview WHERE id = ?", (feed_id,)).fetchone()

    def get_feed_by_url(self, url: str) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM feed_overview WHERE url = ?", (url,)).fetchone()

    def list_feeds(self, *, active_only: bool = False) -> list[sqlite3.Row]:
        sql = "SELECT * FROM feed_overview"
        if active_only:
            sql += " WHERE is_active = 1"
        sql += " ORDER BY COALESCE(NULLIF(title, ''), url) COLLATE NOCASE"
        with self.connect() as conn:
            return conn.execute(sql).fetchall()

    def count_feeds(self) -> int:
        with self.connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM feeds").fetchone()[0])

    def upsert_feed(
        self,
        url: str,
        *,
        title: str | None = None,
        site_url: str | None = None,
        description: str | None = None,
    ) -> int:
        """Insert a feed, or fill in blanks for an existing one. Returns the id."""

        now = utc_now_iso()
        with self.connect() as conn:
            row = conn.execute(
                """
                INSERT INTO feeds (url, title, site_url, description, added_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(url) DO UPDATE SET
                    title       = COALESCE(NULLIF(excluded.title, ''), feeds.title),
                    site_url    = COALESCE(NULLIF(excluded.site_url, ''), feeds.site_url),
                    description = COALESCE(NULLIF(excluded.description, ''), feeds.description),
                    updated_at  = excluded.updated_at
                RETURNING id
                """,
                (url, title, site_url, description, now, now),
            ).fetchone()
            assert row is not None
            return int(row["id"])

    def set_feed_title(self, feed_id: int, title: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE feeds SET title = ?, updated_at = ? WHERE id = ?",
                (title, utc_now_iso(), feed_id),
            )

    def record_fetch_success(
        self,
        feed_id: int,
        *,
        etag: str | None,
        last_modified: str | None,
        title: str | None = None,
        site_url: str | None = None,
        description: str | None = None,
    ) -> None:
        now = utc_now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE feeds SET
                    etag            = COALESCE(?, etag),
                    last_modified   = COALESCE(?, last_modified),
                    title           = COALESCE(NULLIF(title, ''), NULLIF(?, '')),
                    site_url        = COALESCE(NULLIF(site_url, ''), NULLIF(?, '')),
                    description     = COALESCE(NULLIF(description, ''), NULLIF(?, '')),
                    last_fetched_at = ?,
                    last_error      = NULL,
                    updated_at      = ?
                WHERE id = ?
                """,
                (etag, last_modified, title, site_url, description, now, now, feed_id),
            )

    def record_fetch_error(self, feed_id: int, message: str) -> None:
        now = utc_now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE feeds SET last_error = ?, last_fetched_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (message[:1000], now, now, feed_id),
            )

    def delete_feed(self, feed_id: int) -> int:
        with self.connect() as conn:
            cur = conn.execute("DELETE FROM feeds WHERE id = ?", (feed_id,))
            return int(cur.rowcount or 0)

    # ---------------------------------------------------------------- entries

    def insert_entries(self, feed_id: int, entries: Sequence[NewEntry]) -> int:
        """Insert unseen entries, skipping ``(feed_id, guid)`` duplicates.

        Returns the number of newly inserted rows. Existing rows are left alone so
        read/starred flags survive a refresh.
        """

        if not entries:
            return 0
        now = utc_now_iso()
        rows = [
            (
                feed_id,
                entry.guid,
                entry.url,
                entry.title,
                entry.author,
                entry.summary,
                entry.content,
                timestamp_to_iso(entry.published_ts),
                entry.published_ts,
                now,
            )
            for entry in entries
        ]
        with self.connect() as conn:
            before = conn.total_changes
            conn.executemany(
                """
                INSERT INTO entries (
                    feed_id, guid, url, title, author, summary, content,
                    published_at, published_ts, fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (feed_id, guid) DO NOTHING
                """,
                rows,
            )
            return int(conn.total_changes - before)

    def prune_entries(self, feed_id: int, keep: int) -> int:
        """Delete the oldest non-starred entries beyond ``keep``. Returns rows removed."""

        if keep <= 0:
            return 0
        with self.connect() as conn:
            cur = conn.execute(
                """
                DELETE FROM entries
                WHERE feed_id = :feed_id
                  AND is_starred = 0
                  AND id NOT IN (
                      SELECT id FROM entries
                      WHERE feed_id = :feed_id
                      ORDER BY COALESCE(published_ts, 0) DESC, id DESC
                      LIMIT :keep
                  )
                """,
                {"feed_id": feed_id, "keep": keep},
            )
            return int(cur.rowcount or 0)

    def get_entry(self, entry_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM entry_overview WHERE id = ?", (entry_id,)).fetchone()

    def list_entries(
        self,
        *,
        feed_id: int | None = None,
        limit: int = 20,
        offset: int = 0,
        unread_only: bool = False,
        starred_only: bool = False,
        since_ts: int | None = None,
        oldest_first: bool = False,
    ) -> tuple[list[sqlite3.Row], int]:
        """Return one page of entries plus the total number of matches."""

        where: list[str] = []
        params: dict[str, object] = {}
        if feed_id is not None:
            where.append("feed_id = :feed_id")
            params["feed_id"] = feed_id
        if unread_only:
            where.append("is_read = 0")
        if starred_only:
            where.append("is_starred = 1")
        if since_ts is not None:
            where.append("COALESCE(published_ts, 0) >= :since_ts")
            params["since_ts"] = since_ts
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        direction = "ASC" if oldest_first else "DESC"
        params["limit"] = max(1, limit)
        params["offset"] = max(0, offset)
        with self.connect() as conn:
            total = int(
                conn.execute(f"SELECT COUNT(*) FROM entries {clause}", params).fetchone()[0]
            )
            rows = conn.execute(
                f"""
                SELECT * FROM entry_overview {clause}
                ORDER BY COALESCE(published_ts, 0) {direction}, id {direction}
                LIMIT :limit OFFSET :offset
                """,
                params,
            ).fetchall()
        return rows, total

    def search_entries(
        self,
        query: str,
        *,
        feed_id: int | None = None,
        limit: int = 20,
    ) -> tuple[list[sqlite3.Row], int]:
        pattern = f"%{_escape_like(query)}%"
        where = [
            (
                "(title LIKE :pattern ESCAPE '\\' OR summary LIKE :pattern ESCAPE '\\'"
                " OR content LIKE :pattern ESCAPE '\\' OR author LIKE :pattern ESCAPE '\\')"
            )
        ]
        params: dict[str, object] = {"pattern": pattern, "limit": max(1, limit)}
        if feed_id is not None:
            where.append("feed_id = :feed_id")
            params["feed_id"] = feed_id
        clause = " AND ".join(where)
        with self.connect() as conn:
            total = int(
                conn.execute(
                    f"SELECT COUNT(*) FROM entry_overview WHERE {clause}", params
                ).fetchone()[0]
            )
            rows = conn.execute(
                f"""
                SELECT * FROM entry_overview WHERE {clause}
                ORDER BY COALESCE(published_ts, 0) DESC, id DESC
                LIMIT :limit
                """,
                params,
            ).fetchall()
        return rows, total

    def update_entry_flags(
        self,
        *,
        entry_ids: Sequence[int] | None = None,
        feed_id: int | None = None,
        read: bool | None = None,
        starred: bool | None = None,
    ) -> int:
        """Set read/starred flags on explicit entries or on a whole feed."""

        if read is None and starred is None:
            return 0
        sets: list[str] = []
        params: dict[str, object] = {}
        if read is not None:
            sets.append("is_read = :read")
            params["read"] = 1 if read else 0
        if starred is not None:
            sets.append("is_starred = :starred")
            params["starred"] = 1 if starred else 0
        where: list[str] = []
        if entry_ids:
            placeholders = ", ".join(f":id{i}" for i in range(len(entry_ids)))
            where.append(f"id IN ({placeholders})")
            for index, entry_id in enumerate(entry_ids):
                params[f"id{index}"] = int(entry_id)
        if feed_id is not None:
            where.append("feed_id = :feed_id")
            params["feed_id"] = feed_id
        if not where:
            return 0
        with self.connect() as conn:
            cur = conn.execute(
                f"UPDATE entries SET {', '.join(sets)} WHERE {' AND '.join(where)}",
                params,
            )
            return int(cur.rowcount or 0)

    def stats(self) -> dict[str, int]:
        with self.connect() as conn:
            feeds = int(conn.execute("SELECT COUNT(*) FROM feeds").fetchone()[0])
            entries = int(conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0])
            unread = int(
                conn.execute("SELECT COUNT(*) FROM entries WHERE is_read = 0").fetchone()[0]
            )
            starred = int(
                conn.execute("SELECT COUNT(*) FROM entries WHERE is_starred = 1").fetchone()[0]
            )
        return {
            "feeds": feeds,
            "entries": entries,
            "unread": unread,
            "starred": starred,
        }


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def create_database(path: str | Path) -> Database:
    """Create, initialise and return a :class:`Database`."""

    db = Database(path)
    db.initialize()
    return db


def iter_entry_ids(rows: Iterable[sqlite3.Row]) -> Iterator[int]:
    for row in rows:
        yield int(row["id"])
