"""rss-mcp: an MCP server that keeps RSS/Atom subscriptions in SQLite.

The server speaks the Model Context Protocol over **stdio** (stdout carries
JSON-RPC only; all diagnostics go to stderr). It exposes:

Tools
    add_feed, list_feeds, remove_feed, refresh_feed, refresh_all_feeds,
    list_entries, get_entry, search_entries, mark_entries, import_opml,
    export_opml, server_info

Resources
    rss://feeds                      every saved feed as JSON
    rss://feeds/{feed_id}/entries    latest stored entries for one feed
    rss://entries/{entry_id}         one entry with its body
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated
from urllib.parse import urlparse
from xml.etree import ElementTree

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ResourceError, ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from .config import VERSION, Settings
from .db import (
    Database,
    iso_to_timestamp,
    utc_now_iso,
)
from .fetcher import (
    FeedError,
    FetchResult,
    ParsedFeed,
    discover_feed_urls,
    fetch_document,
    parse_feed,
)
from .models import (
    EntryDetail,
    EntryInfo,
    EntryList,
    EntryMutation,
    FeedActionResult,
    FeedInfo,
    FeedList,
    OpmlExportResult,
    OpmlImportResult,
    RefreshReport,
    RefreshSummary,
    ServerInfo,
)
from .textutil import truncate

logger = logging.getLogger("rss_mcp")

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
WRITES_LOCAL = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=True
)
DESTRUCTIVE = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False
)

mcp = MCPServer(
    name="rss-mcp",
    title="RSS feed reader",
    version=VERSION,
    instructions=(
        "Store RSS/Atom subscriptions in a local SQLite file and read them back. "
        "Typical flow: add_feed once, then list_entries / get_entry to read, and "
        "refresh_feed (or refresh_all_feeds) to pull new items."
    ),
)


# --------------------------------------------------------------------------- state

_lock = threading.Lock()
_runtime: tuple[Settings, Database] | None = None


def get_runtime() -> tuple[Settings, Database]:
    """Return the process-wide (settings, database), initialising it on first use."""

    global _runtime
    with _lock:
        if _runtime is None:
            settings = Settings.from_env()
            db = Database(settings.db_path)
            db.initialize()
            _runtime = (settings, db)
            logger.info("using SQLite database at %s", settings.db_path)
        return _runtime


def reset_runtime() -> None:
    """Forget the cached runtime (used by tests that swap the database path)."""

    global _runtime
    with _lock:
        _runtime = None


# ------------------------------------------------------------------- conversion


def _feed_info(row: sqlite3.Row) -> FeedInfo:
    return FeedInfo(
        id=int(row["id"]),
        url=row["url"],
        title=row["title"],
        site_url=row["site_url"],
        description=row["description"],
        is_active=bool(row["is_active"]),
        added_at=row["added_at"],
        last_fetched_at=row["last_fetched_at"],
        last_error=row["last_error"],
        entry_count=int(row["entry_count"]),
        unread_count=int(row["unread_count"]),
    )


def _entry_info(row: sqlite3.Row) -> EntryInfo:
    return EntryInfo(
        id=int(row["id"]),
        feed_id=int(row["feed_id"]),
        feed_title=row["feed_title"],
        title=row["title"],
        url=row["url"],
        author=row["author"],
        published_at=row["published_at"],
        summary=row["summary"],
        is_read=bool(row["is_read"]),
        is_starred=bool(row["is_starred"]),
    )


def _entry_detail(row: sqlite3.Row, *, max_content_chars: int | None) -> EntryDetail:
    body = row["content"] or row["summary"]
    text, truncated = truncate(body, max_content_chars)
    return EntryDetail(
        **_entry_info(row).model_dump(),
        content=text,
        content_truncated=truncated,
        guid=row["guid"],
        fetched_at=row["fetched_at"],
    )


# --------------------------------------------------------------------- loading


@dataclass(slots=True)
class Loaded:
    """A feed document after redirects, HTML discovery and parsing."""

    url: str
    fetch: FetchResult
    parsed: ParsedFeed | None
    discovered_from: str | None = None


async def _load_feed(
    url: str,
    settings: Settings,
    *,
    auto_discover: bool = True,
    etag: str | None = None,
    last_modified: str | None = None,
) -> Loaded:
    fetch = await fetch_document(url, settings, etag=etag, last_modified=last_modified)
    discovered_from: str | None = None

    if not fetch.not_modified and fetch.is_html:
        if not auto_discover:
            raise FeedError(
                f"{fetch.final_url} returned an HTML page, not a feed. "
                "Pass the direct feed URL or enable auto_discover."
            )
        candidates = discover_feed_urls(fetch.body, fetch.final_url)
        if not candidates:
            raise FeedError(f'No RSS/Atom <link rel="alternate"> was found on {fetch.final_url}')
        discovered_from = fetch.final_url
        logger.info("discovered %s on %s", candidates[0], discovered_from)
        fetch = await fetch_document(candidates[0], settings)
        if fetch.not_modified:
            raise FeedError(f"Unexpected 304 Not Modified from discovered feed {candidates[0]}")
        if fetch.is_html:
            raise FeedError(
                f"{fetch.final_url} (discovered on {discovered_from}) is HTML, not a feed"
            )

    parsed = None if fetch.not_modified else parse_feed(fetch.body, settings)
    return Loaded(url=fetch.final_url, fetch=fetch, parsed=parsed, discovered_from=discovered_from)


def _store_entries(db: Database, feed_id: int, parsed: ParsedFeed, settings: Settings) -> int:
    inserted = db.insert_entries(feed_id, parsed.entries)
    db.prune_entries(feed_id, settings.max_entries_per_feed)
    return inserted


async def _add_core(
    url: str,
    settings: Settings,
    db: Database,
    *,
    title: str | None = None,
    auto_discover: bool = True,
) -> FeedActionResult:
    loaded = await _load_feed(url, settings, auto_discover=auto_discover)
    if loaded.parsed is None:  # pragma: no cover - 304 cannot happen on a fresh add
        raise FeedError(f"{loaded.url} answered 304 Not Modified for a feed that was never saved")
    parsed = loaded.parsed

    existing = db.get_feed_by_url(loaded.url)
    feed_id = db.upsert_feed(
        loaded.url,
        title=title or parsed.title,
        site_url=parsed.site_url,
        description=parsed.description,
    )
    inserted = _store_entries(db, feed_id, parsed, settings)
    db.record_fetch_success(
        feed_id,
        etag=loaded.fetch.etag,
        last_modified=loaded.fetch.last_modified,
        title=parsed.title,
        site_url=parsed.site_url,
        description=parsed.description,
    )
    if title:  # an explicit title always wins over the feed's own title
        db.set_feed_title(feed_id, title)
    row = db.get_feed(feed_id)
    assert row is not None
    name = row["title"] or row["url"]
    if existing is None:
        message = f"Subscribed to {name} and stored {inserted} entries"
    else:
        message = f"{name} was already saved; refreshed and stored {inserted} new entries"
    if loaded.discovered_from:
        message += f" (feed URL discovered on {loaded.discovered_from})"
    return FeedActionResult(
        ok=True,
        message=message,
        feed=_feed_info(row),
        new_entries=inserted,
        discovered_from=loaded.discovered_from,
    )


async def _refresh_core(feed_row: sqlite3.Row, settings: Settings, db: Database) -> RefreshReport:
    feed_id = int(feed_row["id"])
    url = feed_row["url"]
    try:
        loaded = await _load_feed(
            url,
            settings,
            auto_discover=False,
            etag=feed_row["etag"],
            last_modified=feed_row["last_modified"],
        )
    except FeedError as exc:
        db.record_fetch_error(feed_id, str(exc))
        return RefreshReport(
            feed_id=feed_id,
            url=url,
            title=feed_row["title"],
            status="failed",
            error=str(exc),
        )

    if loaded.parsed is None:
        db.record_fetch_success(
            feed_id, etag=None, last_modified=None, title=None, site_url=None, description=None
        )
        return RefreshReport(
            feed_id=feed_id,
            url=url,
            title=feed_row["title"],
            status="not_modified",
            http_status=loaded.fetch.status_code,
        )

    parsed = loaded.parsed
    inserted = _store_entries(db, feed_id, parsed, settings)
    db.record_fetch_success(
        feed_id,
        etag=loaded.fetch.etag,
        last_modified=loaded.fetch.last_modified,
        title=parsed.title,
        site_url=parsed.site_url,
        description=parsed.description,
    )
    return RefreshReport(
        feed_id=feed_id,
        url=url,
        title=parsed.title or feed_row["title"],
        status="updated",
        new_entries=inserted,
        http_status=loaded.fetch.status_code,
    )


def _resolve_feed_row(db: Database, feed_id: int | None, url: str | None) -> sqlite3.Row:
    if feed_id is None and not url:
        raise ToolError("Provide either feed_id or url")
    row = db.get_feed(int(feed_id)) if feed_id is not None else db.get_feed_by_url(url or "")
    if row is None:
        target = f"feed_id={feed_id}" if feed_id is not None else f"url={url!r}"
        raise ToolError(f"No saved feed matches {target}. Use list_feeds to see subscriptions.")
    return row


def _parse_since(value: str | None) -> int | None:
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    if text.lstrip("-").isdigit():
        return int(text)
    timestamp = iso_to_timestamp(text)
    if timestamp is None:
        raise ToolError(
            f"Could not parse since={value!r}; use ISO-8601 (2025-01-31) or epoch seconds"
        )
    return timestamp


# ----------------------------------------------------------------------- tools


@mcp.tool(title="Add feed", annotations=WRITES_LOCAL)
async def add_feed(
    url: Annotated[
        str, Field(description="Feed URL, or a site URL when auto_discover finds the feed")
    ],
    title: Annotated[
        str | None, Field(description="Optional display title overriding the feed's own title")
    ] = None,
    auto_discover: Annotated[
        bool,
        Field(description="If the URL returns HTML, look for a <link rel='alternate'> feed URL"),
    ] = True,
) -> FeedActionResult:
    """Subscribe to an RSS/Atom feed, store it in SQLite and keep its current items.

    Adding an already-saved feed refreshes it instead of duplicating it.
    """

    settings, db = get_runtime()
    try:
        return await _add_core(url, settings, db, title=title, auto_discover=auto_discover)
    except FeedError as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool(title="List feeds", annotations=READ_ONLY)
def list_feeds() -> FeedList:
    """List every saved subscription with entry and unread counts."""

    settings, db = get_runtime()
    rows = db.list_feeds()
    return FeedList(
        feeds=[_feed_info(row) for row in rows],
        total=len(rows),
        database=str(Path(settings.db_path).expanduser()),
    )


@mcp.tool(title="Remove feed", annotations=DESTRUCTIVE)
def remove_feed(
    feed_id: Annotated[int | None, Field(description="Id of the feed to delete")] = None,
    url: Annotated[str | None, Field(description="Feed URL to delete, if id is unknown")] = None,
) -> FeedActionResult:
    """Delete a subscription and all of its stored entries."""

    _, db = get_runtime()
    row = _resolve_feed_row(db, feed_id, url)
    name = row["title"] or row["url"]
    entries = int(row["entry_count"])
    removed = db.delete_feed(int(row["id"]))
    if not removed:
        raise ToolError(f"Feed {name} disappeared before it could be deleted")
    return FeedActionResult(
        ok=True,
        message=f"Removed {name} and {entries} stored entries",
        removed_entries=entries,
    )


@mcp.tool(title="Refresh feed", annotations=WRITES_LOCAL)
async def refresh_feed(
    feed_id: Annotated[int | None, Field(description="Id of the feed to refresh")] = None,
    url: Annotated[str | None, Field(description="URL of the feed to refresh")] = None,
) -> FeedActionResult:
    """Fetch one saved feed again, using ETag/Last-Modified when the server sent them."""

    settings, db = get_runtime()
    row = _resolve_feed_row(db, feed_id, url)
    report = await _refresh_core(row, settings, db)
    if report.status == "failed":
        raise ToolError(report.error or "refresh failed")
    updated = db.get_feed(int(row["id"]))
    assert updated is not None
    detail = (
        "already up to date"
        if report.status == "not_modified"
        else f"{report.new_entries} new entries"
    )
    return FeedActionResult(
        ok=True,
        message=f"Refreshed {updated['title'] or updated['url']}: {detail}",
        feed=_feed_info(updated),
        new_entries=report.new_entries,
    )


@mcp.tool(title="Refresh all feeds", annotations=WRITES_LOCAL)
async def refresh_all_feeds() -> RefreshSummary:
    """Refresh every saved feed, collecting per-feed successes and failures."""

    settings, db = get_runtime()
    rows = db.list_feeds(active_only=True)
    reports: list[RefreshReport] = []
    for row in rows:
        reports.append(await _refresh_core(row, settings, db))
    return RefreshSummary(
        results=reports,
        refreshed=sum(1 for report in reports if report.status != "failed"),
        failed=sum(1 for report in reports if report.status == "failed"),
        new_entries=sum(report.new_entries for report in reports),
    )


@mcp.tool(title="List entries", annotations=READ_ONLY)
def list_entries(
    feed_id: Annotated[int | None, Field(description="Restrict to one feed")] = None,
    limit: Annotated[int, Field(ge=1, le=200, description="Entries per page")] = 20,
    offset: Annotated[int, Field(ge=0, description="Entries to skip, for paging")] = 0,
    unread_only: Annotated[bool, Field(description="Only entries not yet marked read")] = False,
    starred_only: Annotated[bool, Field(description="Only starred entries")] = False,
    since: Annotated[
        str | None,
        Field(description="Only entries published on/after this ISO-8601 date or epoch seconds"),
    ] = None,
    oldest_first: Annotated[bool, Field(description="Sort ascending by publication time")] = False,
) -> EntryList:
    """Read stored entries newest-first, with paging and unread/starred filters."""

    _, db = get_runtime()
    since_ts = _parse_since(since)
    rows, total = db.list_entries(
        feed_id=feed_id,
        limit=limit,
        offset=offset,
        unread_only=unread_only,
        starred_only=starred_only,
        since_ts=since_ts,
        oldest_first=oldest_first,
    )
    entries = [_entry_info(row) for row in rows]
    return EntryList(
        entries=entries,
        returned=len(entries),
        total=total,
        offset=offset,
        has_more=offset + len(entries) < total,
    )


@mcp.tool(title="Get entry", annotations=READ_ONLY)
def get_entry(
    entry_id: Annotated[int, Field(description="Entry id from list_entries or search_entries")],
    include_content: Annotated[bool, Field(description="Include the item body")] = True,
    max_content_chars: Annotated[
        int, Field(ge=200, le=100_000, description="Truncate the body to this many characters")
    ] = 4000,
    mark_read: Annotated[bool, Field(description="Mark the entry read as a side effect")] = True,
) -> EntryDetail:
    """Return one entry in full, optionally marking it read."""

    _, db = get_runtime()
    row = db.get_entry(entry_id)
    if row is None:
        raise ToolError(f"No entry with id {entry_id}")
    if mark_read and not row["is_read"]:
        db.update_entry_flags(entry_ids=[entry_id], read=True)
        row = db.get_entry(entry_id)
        assert row is not None
    detail = _entry_detail(row, max_content_chars=max_content_chars if include_content else 0)
    if not include_content:
        detail.content = None
        detail.content_truncated = False
    return detail


@mcp.tool(title="Search entries", annotations=READ_ONLY)
def search_entries(
    query: Annotated[str, Field(min_length=1, description="Text to match in title/summary/body")],
    feed_id: Annotated[int | None, Field(description="Restrict the search to one feed")] = None,
    limit: Annotated[int, Field(ge=1, le=200, description="Maximum hits")] = 20,
) -> EntryList:
    """Case-insensitive substring search across stored entries."""

    _, db = get_runtime()
    needle = query.strip()
    if not needle:
        raise ToolError("query must not be empty")
    rows, total = db.search_entries(needle, feed_id=feed_id, limit=limit)
    entries = [_entry_info(row) for row in rows]
    return EntryList(entries=entries, returned=len(entries), total=total, offset=0, has_more=False)


@mcp.tool(title="Mark entries", annotations=WRITES_LOCAL)
def mark_entries(
    entry_ids: Annotated[
        list[int] | None, Field(description="Entries to update; omit to target a whole feed")
    ] = None,
    feed_id: Annotated[
        int | None, Field(description="Restrict the update to one feed (used for mark-all-read)")
    ] = None,
    read: Annotated[bool | None, Field(description="Set the read flag")] = None,
    starred: Annotated[bool | None, Field(description="Set the starred flag")] = None,
) -> EntryMutation:
    """Set read/starred flags on specific entries, a whole feed, or both combined."""

    _, db = get_runtime()
    if read is None and starred is None:
        raise ToolError("Provide read and/or starred")
    if not entry_ids and feed_id is None:
        raise ToolError("Provide entry_ids and/or feed_id")
    affected = db.update_entry_flags(
        entry_ids=entry_ids, feed_id=feed_id, read=read, starred=starred
    )
    parts = []
    if read is not None:
        parts.append("read" if read else "unread")
    if starred is not None:
        parts.append("starred" if starred else "unstarred")
    scope = (
        f"feed {feed_id}"
        if feed_id is not None and not entry_ids
        else (f"{len(entry_ids or [])} entries")
    )
    return EntryMutation(
        ok=True, message=f"Marked {affected} of {scope} as {' and '.join(parts)}", affected=affected
    )


@mcp.tool(title="Import OPML", annotations=WRITES_LOCAL)
async def import_opml(
    source: Annotated[str, Field(description="Path to an .opml/.xml file, or an http(s) URL")],
    limit: Annotated[int, Field(ge=1, le=500, description="Maximum feeds to import")] = 100,
) -> OpmlImportResult:
    """Import the subscriptions listed in an OPML document."""

    settings, db = get_runtime()
    raw = await _read_opml_source(source, settings)
    urls = _opml_feed_urls(raw)
    if not urls:
        raise ToolError(f"No xmlUrl outlines found in {source}")

    imported = 0
    skipped = 0
    failed: list[str] = []
    for url in urls[:limit]:
        if db.get_feed_by_url(url) is not None:
            skipped += 1
            continue
        try:
            await _add_core(url, settings, db, auto_discover=False)
            imported += 1
        except FeedError as exc:
            failed.append(f"{url}: {exc}")
        except Exception as exc:  # noqa: BLE001 - one bad feed must not abort the import
            failed.append(f"{url}: {type(exc).__name__}: {exc}")
    return OpmlImportResult(
        ok=True,
        message=(
            f"Imported {imported} feeds from {source} "
            f"({skipped} already saved, {len(failed)} failed)"
        ),
        imported=imported,
        skipped=skipped,
        failed=failed,
    )


@mcp.tool(title="Export OPML", annotations=READ_ONLY)
def export_opml(
    path: Annotated[
        str, Field(description="Destination file path for the OPML document (parent dirs created)")
    ] = "feeds.opml",
) -> OpmlExportResult:
    """Write every saved subscription to an OPML 2.0 document."""

    _, db = get_runtime()
    rows = db.list_feeds()
    payload = _build_opml(rows)
    destination = Path(path).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(payload, encoding="utf-8")
    return OpmlExportResult(
        ok=True,
        message=f"Exported {len(rows)} feeds to {destination}",
        path=str(destination.resolve()),
        feeds=len(rows),
    )


@mcp.tool(title="Server info", annotations=READ_ONLY)
def server_info() -> ServerInfo:
    """Report version, database location and storage counters."""

    settings, db = get_runtime()
    stats = db.stats()
    return ServerInfo(
        name="rss-mcp",
        version=VERSION,
        database=str(Path(settings.db_path).expanduser()),
        transport="stdio",
        feeds=stats["feeds"],
        entries=stats["entries"],
        unread=stats["unread"],
        starred=stats["starred"],
    )


# -------------------------------------------------------------------- OPML I/O


async def _read_opml_source(source: str, settings: Settings) -> bytes:
    parsed = urlparse(source)
    if parsed.scheme in {"http", "https"}:
        try:
            fetch = await fetch_document(source, settings)
        except FeedError as exc:
            raise ToolError(str(exc)) from exc
        return fetch.body
    path = Path(source).expanduser()
    if not path.is_file():
        raise ToolError(f"OPML file not found: {path}")
    return path.read_bytes()


def _opml_feed_urls(raw: bytes) -> list[str]:
    try:
        root = ElementTree.fromstring(raw)
    except ElementTree.ParseError as exc:
        raise ToolError(f"Invalid OPML/XML: {exc}") from exc
    urls: list[str] = []
    seen: set[str] = set()
    for outline in root.iter("outline"):
        url = (outline.get("xmlUrl") or outline.get("xmlurl") or "").strip()
        if url and url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def _build_opml(rows: Sequence[sqlite3.Row]) -> str:
    opml = ElementTree.Element("opml", {"version": "2.0"})
    head = ElementTree.SubElement(opml, "head")
    ElementTree.SubElement(head, "title").text = "rss-mcp subscriptions"
    ElementTree.SubElement(head, "dateCreated").text = utc_now_iso()
    body = ElementTree.SubElement(opml, "body")
    for row in rows:
        label = row["title"] or row["url"]
        attrs = {"type": "rss", "text": label, "title": label, "xmlUrl": row["url"]}
        if row["site_url"]:
            attrs["htmlUrl"] = row["site_url"]
        ElementTree.SubElement(body, "outline", attrs)
    ElementTree.indent(opml)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ElementTree.tostring(
        opml, encoding="unicode"
    )


# ------------------------------------------------------------------- resources


@mcp.resource(
    "rss://feeds",
    name="feeds",
    title="Saved feeds",
    description="Every saved subscription as JSON, with entry and unread counts.",
    mime_type="application/json",
)
def feeds_resource() -> str:
    settings, db = get_runtime()
    rows = db.list_feeds()
    return json.dumps(
        {
            "database": str(settings.db_path),
            "total": len(rows),
            "feeds": [_feed_info(row).model_dump() for row in rows],
        },
        indent=2,
        ensure_ascii=False,
    )


@mcp.resource(
    "rss://feeds/{feed_id}/entries",
    name="feed-entries",
    title="Entries of one feed",
    description="The 20 most recent stored entries for a feed, as JSON.",
    mime_type="application/json",
)
def feed_entries_resource(feed_id: str) -> str:
    _, db = get_runtime()
    try:
        parsed_id = int(feed_id)
    except (TypeError, ValueError) as exc:
        raise ResourceError(f"feed_id must be an integer, got {feed_id!r}") from exc
    row = db.get_feed(parsed_id)
    if row is None:
        raise ResourceError(f"No feed with id {parsed_id}")
    entries, total = db.list_entries(feed_id=parsed_id, limit=20)
    return json.dumps(
        {
            "feed": _feed_info(row).model_dump(),
            "total": total,
            "entries": [_entry_info(entry).model_dump() for entry in entries],
        },
        indent=2,
        ensure_ascii=False,
    )


@mcp.resource(
    "rss://entries/{entry_id}",
    name="entry",
    title="Single entry",
    description="One stored entry with its body, as JSON. Read-only: does not change flags.",
    mime_type="application/json",
)
def entry_resource(entry_id: str) -> str:
    _, db = get_runtime()
    try:
        parsed_id = int(entry_id)
    except (TypeError, ValueError) as exc:
        raise ResourceError(f"entry_id must be an integer, got {entry_id!r}") from exc
    row = db.get_entry(parsed_id)
    if row is None:
        raise ResourceError(f"No entry with id {parsed_id}")
    detail = _entry_detail(row, max_content_chars=20_000)
    return json.dumps(detail.model_dump(), indent=2, ensure_ascii=False)


# ------------------------------------------------------------------------- CLI


def _configure_logging(level: str) -> None:
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger("rss_mcp")
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
    root.propagate = False
    # third-party chatter would otherwise spam the client's stderr at INFO
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="rss-mcp",
        description="MCP server exposing SQLite-backed RSS/Atom feeds over stdio.",
    )
    parser.add_argument(
        "--db",
        metavar="PATH",
        help="SQLite file to use (default: $RSS_MCP_DB or ~/.local/share/rss-mcp/feeds.db)",
    )
    parser.add_argument(
        "--transport",
        choices=("stdio", "sse", "streamable-http"),
        default="stdio",
        help="Transport to serve (default: stdio)",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Host for HTTP transports")
    parser.add_argument("--port", type=int, default=8000, help="Port for HTTP transports")
    parser.add_argument(
        "--log-level",
        default=os.environ.get("RSS_MCP_LOG_LEVEL", "INFO"),
        choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
        help="stderr log level (default: INFO)",
    )
    parser.add_argument("--version", action="version", version=f"rss-mcp {VERSION}")
    args = parser.parse_args(argv)

    if args.db:
        os.environ["RSS_MCP_DB"] = args.db
    _configure_logging(args.log_level)

    settings, db = get_runtime()
    stats = db.stats()
    logger.info(
        "rss-mcp %s ready on %s (db=%s, feeds=%d, entries=%d)",
        VERSION,
        args.transport,
        settings.db_path,
        stats["feeds"],
        stats["entries"],
    )

    if args.transport == "stdio":
        mcp.run(transport="stdio")
    else:  # pragma: no cover - optional HTTP transports
        mcp.run(transport=args.transport, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
