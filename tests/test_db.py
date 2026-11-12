"""Unit tests for the SQLite layer -- no HTTP, no MCP."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from rss_mcp.db import Database, NewEntry, iso_to_timestamp, struct_to_timestamp

FEED_URL = "http://example.test/rss.xml"


def _entry(guid: str, *, ts: int | None = 1736157600, title: str | None = None) -> NewEntry:
    return NewEntry(
        guid=guid,
        url=f"http://example.test/{guid}",
        title=title or f"Entry {guid}",
        author="Ada",
        summary="summary text",
        content="<p>content</p>",
        published_ts=ts,
    )


def test_initialize_creates_schema_and_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "feeds.db"
    db = Database(path)
    db.initialize()
    db.initialize()  # second call must not raise
    assert path.exists()
    with db.connect() as conn:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")
        }
        assert {"feeds", "entries", "feed_overview", "entry_overview"} <= tables
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1


def test_upsert_feed_fills_blanks_without_duplicating(db: Database) -> None:
    feed_id = db.upsert_feed(FEED_URL, title="First title", site_url="http://example.test/")
    again = db.upsert_feed(FEED_URL, title=None, description="desc")
    assert feed_id == again
    assert db.count_feeds() == 1
    row = db.get_feed_by_url(FEED_URL)
    assert row is not None
    assert row["title"] == "First title"
    assert row["description"] == "desc"


def test_insert_entries_deduplicates_on_guid(db: Database) -> None:
    feed_id = db.upsert_feed(FEED_URL)
    assert db.insert_entries(feed_id, [_entry("a"), _entry("b")]) == 2
    # same guids again -> nothing inserted
    assert db.insert_entries(feed_id, [_entry("a"), _entry("b"), _entry("c")]) == 1
    rows, total = db.list_entries(feed_id=feed_id, limit=10)
    assert total == 3
    assert len(rows) == 3


def test_refresh_does_not_clobber_read_and_starred_flags(db: Database) -> None:
    feed_id = db.upsert_feed(FEED_URL)
    db.insert_entries(feed_id, [_entry("a")])
    row = db.list_entries(feed_id=feed_id, limit=1)[0][0]
    db.update_entry_flags(entry_ids=[row["id"]], read=True, starred=True)

    db.insert_entries(feed_id, [_entry("a", title="Changed upstream")])
    refreshed = db.get_entry(row["id"])
    assert refreshed is not None
    assert bool(refreshed["is_read"]) is True
    assert bool(refreshed["is_starred"]) is True
    assert refreshed["title"] == "Entry a"


def test_ordering_and_paging(db: Database) -> None:
    feed_id = db.upsert_feed(FEED_URL)
    db.insert_entries(
        feed_id,
        [_entry("old", ts=1736157600), _entry("mid", ts=1736244000), _entry("new", ts=1736330400)],
    )
    newest_first, total = db.list_entries(feed_id=feed_id, limit=2)
    assert total == 3
    assert [row["guid"] for row in newest_first] == ["new", "mid"]

    page_two, _ = db.list_entries(feed_id=feed_id, limit=2, offset=2)
    assert [row["guid"] for row in page_two] == ["old"]

    oldest_first, _ = db.list_entries(feed_id=feed_id, limit=1, oldest_first=True)
    assert [row["guid"] for row in oldest_first] == ["old"]


def test_entry_without_date_sorts_last(db: Database) -> None:
    feed_id = db.upsert_feed(FEED_URL)
    db.insert_entries(feed_id, [_entry("dated", ts=1736157600), _entry("undated", ts=None)])
    rows, _ = db.list_entries(feed_id=feed_id, limit=10)
    assert [row["guid"] for row in rows] == ["dated", "undated"]


def test_unread_and_starred_filters(db: Database) -> None:
    feed_id = db.upsert_feed(FEED_URL)
    db.insert_entries(feed_id, [_entry("a"), _entry("b"), _entry("c")])
    first = db.list_entries(feed_id=feed_id, limit=1)[0][0]
    db.update_entry_flags(entry_ids=[first["id"]], read=True)
    db.update_entry_flags(entry_ids=[first["id"]], starred=True)

    assert db.list_entries(feed_id=feed_id, unread_only=True, limit=10)[1] == 2
    assert db.list_entries(feed_id=feed_id, starred_only=True, limit=10)[1] == 1
    assert db.list_entries(feed_id=feed_id, since_ts=1736244000, limit=10)[1] == 0


def test_mark_whole_feed_read(db: Database) -> None:
    feed_id = db.upsert_feed(FEED_URL)
    db.insert_entries(feed_id, [_entry("a"), _entry("b")])
    assert db.update_entry_flags(feed_id=feed_id, read=True) == 2
    row = db.get_feed(feed_id)
    assert row is not None
    assert row["unread_count"] == 0
    assert row["entry_count"] == 2


def test_search_matches_title_summary_and_content(db: Database) -> None:
    feed_id = db.upsert_feed(FEED_URL)
    db.insert_entries(
        feed_id,
        [
            NewEntry(guid="1", title="SQLite tips", summary="about databases"),
            NewEntry(guid="2", title="Cooking", summary="pasta", content="<p>sqlite</p>"),
            NewEntry(guid="3", title="Unrelated", summary="nothing here"),
        ],
    )
    rows, total = db.search_entries("sqlite", limit=10)
    assert total == 2
    assert {row["guid"] for row in rows} == {"1", "2"}


def test_search_treats_wildcards_literally(db: Database) -> None:
    feed_id = db.upsert_feed(FEED_URL)
    db.insert_entries(
        feed_id, [NewEntry(guid="1", title="100% organic"), NewEntry(guid="2", title="100 organic")]
    )
    rows, total = db.search_entries("100%", limit=10)
    assert total == 1
    assert rows[0]["guid"] == "1"


def test_prune_keeps_newest_and_never_deletes_starred(db: Database) -> None:
    feed_id = db.upsert_feed(FEED_URL)
    db.insert_entries(
        feed_id,
        [_entry("old", ts=1000), _entry("mid", ts=2000), _entry("new", ts=3000)],
    )
    old_row = db.list_entries(feed_id=feed_id, limit=1, oldest_first=True)[0][0]
    db.update_entry_flags(entry_ids=[old_row["id"]], starred=True)

    removed = db.prune_entries(feed_id, keep=2)
    assert removed == 0  # the only candidate was starred
    assert db.list_entries(feed_id=feed_id, limit=10)[1] == 3

    db.update_entry_flags(entry_ids=[old_row["id"]], starred=False)
    removed = db.prune_entries(feed_id, keep=2)
    assert removed == 1
    remaining, total = db.list_entries(feed_id=feed_id, limit=10)
    assert total == 2
    assert {row["guid"] for row in remaining} == {"mid", "new"}


def test_delete_feed_cascades_entries(db: Database) -> None:
    feed_id = db.upsert_feed(FEED_URL)
    db.insert_entries(feed_id, [_entry("a")])
    assert db.delete_feed(feed_id) == 1
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 0


def test_record_fetch_success_and_error(db: Database) -> None:
    feed_id = db.upsert_feed(FEED_URL)
    db.record_fetch_success(
        feed_id, etag='"v1"', last_modified="Wed, 08 Jan 2025 09:15:00 GMT", title="T"
    )
    row = db.get_feed(feed_id)
    assert row is not None
    assert row["etag"] == '"v1"'
    assert row["last_error"] is None
    assert row["last_fetched_at"]

    db.record_fetch_error(feed_id, "boom")
    row = db.get_feed(feed_id)
    assert row is not None
    assert row["last_error"] == "boom"
    # a later success clears the error but keeps validators when not resent
    db.record_fetch_success(feed_id, etag=None, last_modified=None)
    row = db.get_feed(feed_id)
    assert row is not None
    assert row["last_error"] is None
    assert row["etag"] == '"v1"'


def test_stats(db: Database) -> None:
    feed_id = db.upsert_feed(FEED_URL)
    db.insert_entries(feed_id, [_entry("a"), _entry("b")])
    db.update_entry_flags(feed_id=feed_id, read=True)
    db.update_entry_flags(feed_id=feed_id, starred=True)
    assert db.stats() == {"feeds": 1, "entries": 2, "unread": 0, "starred": 2}


def test_schema_version_guard(tmp_path: Path) -> None:
    path = tmp_path / "future.db"
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA user_version = 99")
    conn.commit()
    conn.close()
    with pytest.raises(RuntimeError, match="schema v99"):
        Database(path).initialize()


def test_timestamp_helpers() -> None:
    assert iso_to_timestamp("2025-01-06T10:00:00Z") == 1736157600
    assert iso_to_timestamp("2025-01-06") == 1736121600
    assert iso_to_timestamp("not a date") is None
    assert struct_to_timestamp(None) is None
    assert struct_to_timestamp((2025, 1, 6, 10, 0, 0, 0, 6, 0)) == 1736157600
