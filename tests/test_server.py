"""End-to-end tests: a real MCP client, a real stdio subprocess, a real SQLite file.

Nothing is mocked except the feed host (a local HTTP server), so these tests
cover the whole path a real client exercises: JSON-RPC over stdio -> tool ->
HTTP fetch -> SQLite -> structured result.

The client context manager is opened *inside* each test (never in a fixture) so
the anyio task group that owns the subprocess lives and dies in one task.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from rss_mcp.config import VERSION

BASE_ENV = {
    "RSS_MCP_ALLOW_PRIVATE_HOSTS": "1",
    "RSS_MCP_LOG_LEVEL": "WARNING",
}


@asynccontextmanager
async def connect(db_file: Path) -> AsyncIterator[ClientSession]:
    """Spawn ``python -m rss_mcp`` and return an initialised client session."""

    env = {**os.environ, **BASE_ENV, "RSS_MCP_DB": str(db_file)}
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "rss_mcp", "--db", str(db_file)],
        env=env,
    )
    async with (
        stdio_client(params) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as client,
    ):
        await client.initialize()
        yield client


async def call(client: ClientSession, name: str, /, **arguments: object) -> dict:
    """Call a tool and return its structured content, failing on tool errors."""

    result = await client.call_tool(name, arguments)
    if result.is_error:
        message = result.content[0].text if result.content else "unknown error"
        raise AssertionError(f"{name} failed: {message}")
    assert result.structured_content is not None, f"{name} returned no structured content"
    return result.structured_content


async def call_error(client: ClientSession, name: str, /, **arguments: object) -> str:
    """Call a tool expecting a tool-level error; return the error text."""

    result = await client.call_tool(name, arguments)
    assert result.is_error, f"{name} unexpectedly succeeded"
    return result.content[0].text if result.content else ""


# ------------------------------------------------------------------ discovery


async def test_lists_tools_and_resources(db_path: Path) -> None:
    async with connect(db_path) as client:
        tools = await client.list_tools()
        names = {tool.name for tool in tools.tools}
        assert names == {
            "add_feed",
            "list_feeds",
            "remove_feed",
            "refresh_feed",
            "refresh_all_feeds",
            "list_entries",
            "get_entry",
            "search_entries",
            "mark_entries",
            "import_opml",
            "export_opml",
            "server_info",
        }
        # every tool must advertise a structured output schema
        assert all(tool.output_schema for tool in tools.tools)

        resources = await client.list_resources()
        assert {str(resource.uri) for resource in resources.resources} == {"rss://feeds"}
        templates = await client.list_resource_templates()
        assert {template.uri_template for template in templates.resource_templates} == {
            "rss://feeds/{feed_id}/entries",
            "rss://entries/{entry_id}",
        }


async def test_server_info_reports_fresh_database(db_path: Path) -> None:
    async with connect(db_path) as client:
        info = await call(client, "server_info")
        assert info["name"] == "rss-mcp"
        assert info["version"] == VERSION
        assert info["transport"] == "stdio"
        assert info["feeds"] == 0 and info["entries"] == 0
        assert Path(info["database"]) == db_path


# --------------------------------------------------------------- add & read


async def test_add_feed_stores_entries(feed_host: str, db_path: Path) -> None:
    async with connect(db_path) as client:
        result = await call(client, "add_feed", url=f"{feed_host}/rss.xml")
        assert result["ok"] is True
        assert result["new_entries"] == 3
        assert result["feed"]["title"] == "Example Feed"
        assert result["feed"]["entry_count"] == 3
        assert result["feed"]["unread_count"] == 3
        assert result["discovered_from"] is None
        assert db_path.exists()

        feeds = await call(client, "list_feeds")
        assert feeds["total"] == 1
        assert feeds["feeds"][0]["url"].endswith("/rss.xml")

        # adding the same feed again refreshes instead of duplicating
        again = await call(client, "add_feed", url=f"{feed_host}/rss.xml")
        assert again["ok"] is True
        assert again["new_entries"] == 0
        assert len((await call(client, "list_feeds"))["feeds"]) == 1


async def test_add_feed_discovers_feed_from_html_page(feed_host: str, db_path: Path) -> None:
    async with connect(db_path) as client:
        result = await call(client, "add_feed", url=f"{feed_host}/blog/")
        assert result["ok"] is True
        assert result["discovered_from"].endswith("/blog/")
        assert result["feed"]["url"].endswith("/rss.xml")
        assert result["new_entries"] == 3


async def test_add_feed_with_custom_title(feed_host: str, db_path: Path) -> None:
    async with connect(db_path) as client:
        result = await call(client, "add_feed", url=f"{feed_host}/atom.xml", title="My Atom")
        assert result["feed"]["title"] == "My Atom"
        assert result["feed"]["entry_count"] == 2
        # a later refresh must not overwrite the explicit title
        await call(client, "refresh_feed", url=f"{feed_host}/atom.xml")
        assert (await call(client, "list_feeds"))["feeds"][0]["title"] == "My Atom"


async def test_add_feed_surfaces_errors_as_tool_errors(feed_host: str, db_path: Path) -> None:
    async with connect(db_path) as client:
        assert "404" in await call_error(client, "add_feed", url=f"{feed_host}/missing.xml")
        assert "No RSS/Atom" in await call_error(client, "add_feed", url=f"{feed_host}/no-feed")
        assert "scheme" in await call_error(client, "add_feed", url="ftp://example.test/feed.xml")


async def test_list_entries_orders_filters_and_pages(feed_host: str, db_path: Path) -> None:
    async with connect(db_path) as client:
        await call(client, "add_feed", url=f"{feed_host}/rss.xml")

        page = await call(client, "list_entries", limit=2)
        assert page["returned"] == 2
        assert page["total"] == 3
        assert page["has_more"] is True
        assert [entry["title"] for entry in page["entries"]] == [
            "Third post",
            "Second post about SQLite",
        ]
        assert page["entries"][0]["published_at"] == "2025-01-08T09:15:00Z"
        assert page["entries"][0]["feed_title"] == "Example Feed"

        page_two = await call(client, "list_entries", limit=2, offset=2)
        assert page_two["returned"] == 1
        assert page_two["has_more"] is False

        oldest = await call(client, "list_entries", limit=1, oldest_first=True)
        assert oldest["entries"][0]["title"] == "First post"

        since = await call(client, "list_entries", since="2025-01-08", limit=10)
        assert [entry["title"] for entry in since["entries"]] == ["Third post"]

        assert "Could not parse since" in await call_error(
            client, "list_entries", since="not-a-date"
        )

        feed_id = (await call(client, "list_feeds"))["feeds"][0]["id"]
        scoped = await call(client, "list_entries", feed_id=feed_id, limit=10)
        assert scoped["total"] == 3
        empty = await call(client, "list_entries", feed_id=feed_id + 99, limit=10)
        assert empty["total"] == 0 and empty["entries"] == []


async def test_get_entry_returns_body_and_marks_read(feed_host: str, db_path: Path) -> None:
    async with connect(db_path) as client:
        await call(client, "add_feed", url=f"{feed_host}/rss.xml")
        page = await call(client, "list_entries", limit=10, oldest_first=True)
        first = page["entries"][0]

        detail = await call(client, "get_entry", entry_id=first["id"])
        assert detail["title"] == "First post"
        assert detail["guid"] == "post-1"
        assert "Hello <b>world</b>" in detail["content"]
        assert "evil()" not in detail["content"]
        assert detail["content_truncated"] is False
        assert detail["is_read"] is True  # marked read by default

        unread = await call(client, "list_entries", unread_only=True, limit=10)
        assert unread["total"] == 2

        # lean read: no body, no side effects
        second = page["entries"][1]
        lean = await call(
            client, "get_entry", entry_id=second["id"], include_content=False, mark_read=False
        )
        assert lean["content"] is None
        assert lean["content_truncated"] is False
        assert lean["is_read"] is False
        assert (await call(client, "list_entries", unread_only=True, limit=10))["total"] == 2

        assert "No entry" in await call_error(client, "get_entry", entry_id=999_999)


async def test_search_entries(feed_host: str, db_path: Path) -> None:
    async with connect(db_path) as client:
        await call(client, "add_feed", url=f"{feed_host}/rss.xml")
        hits = await call(client, "search_entries", query="sqlite")
        assert hits["total"] == 1
        assert hits["entries"][0]["title"] == "Second post about SQLite"

        misses = await call(client, "search_entries", query="kubernetes")
        assert misses["total"] == 0

        assert "must not be empty" in await call_error(client, "search_entries", query="   ")


async def test_mark_entries_flags(feed_host: str, db_path: Path) -> None:
    async with connect(db_path) as client:
        await call(client, "add_feed", url=f"{feed_host}/rss.xml")
        entries = (await call(client, "list_entries", limit=10))["entries"]
        target = entries[0]["id"]
        feed_id = entries[0]["feed_id"]

        starred = await call(client, "mark_entries", entry_ids=[target], starred=True)
        assert starred["affected"] == 1
        assert (await call(client, "list_entries", starred_only=True, limit=10))["total"] == 1

        marked = await call(client, "mark_entries", feed_id=feed_id, read=True)
        assert marked["affected"] == 3
        assert (await call(client, "list_feeds"))["feeds"][0]["unread_count"] == 0

        assert "read and/or starred" in await call_error(client, "mark_entries", entry_ids=[target])
        assert "entry_ids" in await call_error(client, "mark_entries", read=True)


# ------------------------------------------------------------------ refresh


async def test_refresh_uses_conditional_requests(feed_host: str, db_path: Path) -> None:
    async with connect(db_path) as client:
        await call(client, "add_feed", url=f"{feed_host}/rss.xml")
        refreshed = await call(client, "refresh_feed", url=f"{feed_host}/rss.xml")
        assert refreshed["ok"] is True
        assert refreshed["new_entries"] == 0
        assert "already up to date" in refreshed["message"]

        everything = await call(client, "refresh_all_feeds")
        assert everything["refreshed"] == 1
        assert everything["failed"] == 0
        assert everything["results"][0]["status"] == "not_modified"
        assert everything["results"][0]["http_status"] == 304


async def test_refresh_unknown_feed_is_a_tool_error(feed_host: str, db_path: Path) -> None:
    async with connect(db_path) as client:
        assert "No saved feed" in await call_error(
            client, "refresh_feed", url=f"{feed_host}/rss.xml"
        )


async def test_remove_feed(feed_host: str, db_path: Path) -> None:
    async with connect(db_path) as client:
        await call(client, "add_feed", url=f"{feed_host}/rss.xml")
        feed_id = (await call(client, "list_feeds"))["feeds"][0]["id"]
        removed = await call(client, "remove_feed", feed_id=feed_id)
        assert removed["ok"] is True
        assert removed["removed_entries"] == 3
        assert (await call(client, "list_feeds"))["total"] == 0
        assert (await call(client, "list_entries", limit=10))["total"] == 0
        assert "No saved feed" in await call_error(client, "remove_feed", feed_id=feed_id)


# --------------------------------------------------------------------- OPML


async def test_export_and_import_opml(feed_host: str, db_path: Path, tmp_path: Path) -> None:
    destination = tmp_path / "export" / "feeds.opml"
    async with connect(db_path) as client:
        await call(client, "add_feed", url=f"{feed_host}/rss.xml")
        await call(client, "add_feed", url=f"{feed_host}/atom.xml")

        exported = await call(client, "export_opml", path=str(destination))
        assert exported["feeds"] == 2
        assert destination.is_file()
        text = destination.read_text(encoding="utf-8")
        assert text.startswith("<?xml")
        assert f'xmlUrl="{feed_host}/rss.xml"' in text

        again = await call(client, "import_opml", source=str(destination))
        assert again["imported"] == 0
        assert again["skipped"] == 2

    # importing into a different database adds both feeds
    async with connect(tmp_path / "other.db") as other:
        imported = await call(other, "import_opml", source=str(destination))
        assert imported["imported"] == 2
        assert imported["skipped"] == 0
        assert imported["failed"] == []
        feeds = await call(other, "list_feeds")
        assert {feed["url"] for feed in feeds["feeds"]} == {
            f"{feed_host}/rss.xml",
            f"{feed_host}/atom.xml",
        }


async def test_import_opml_errors(db_path: Path, tmp_path: Path) -> None:
    async with connect(db_path) as client:
        assert "not found" in await call_error(
            client, "import_opml", source=str(tmp_path / "nope.opml")
        )
        broken = tmp_path / "broken.opml"
        broken.write_text("<opml><body>", encoding="utf-8")
        assert "Invalid OPML" in await call_error(client, "import_opml", source=str(broken))


# ---------------------------------------------------------------- resources


async def test_resources_expose_saved_data(feed_host: str, db_path: Path) -> None:
    async with connect(db_path) as client:
        await call(client, "add_feed", url=f"{feed_host}/rss.xml")
        feed_id = (await call(client, "list_feeds"))["feeds"][0]["id"]
        entry_id = (await call(client, "list_entries", limit=1))["entries"][0]["id"]

        feeds_uri = (await client.list_resources()).resources[0].uri
        feeds = json.loads((await client.read_resource(feeds_uri)).contents[0].text)
        assert feeds["total"] == 1
        assert feeds["feeds"][0]["title"] == "Example Feed"

        per_feed = json.loads(
            (await client.read_resource(f"rss://feeds/{feed_id}/entries")).contents[0].text
        )
        assert per_feed["total"] == 3
        assert len(per_feed["entries"]) == 3

        single = json.loads(
            (await client.read_resource(f"rss://entries/{entry_id}")).contents[0].text
        )
        assert single["id"] == entry_id
        assert single["content"]
        # reading a resource must not flip the read flag
        assert (await call(client, "list_entries", unread_only=True, limit=10))["total"] == 3


async def test_resource_errors_for_unknown_ids(db_path: Path) -> None:
    async with connect(db_path) as client:
        with pytest.raises(Exception) as excinfo:
            await client.read_resource("rss://feeds/999/entries")
        assert "999" in str(excinfo.value) or "not found" in str(excinfo.value).lower()

        with pytest.raises(Exception):
            await client.read_resource("rss://entries/not-an-int")
