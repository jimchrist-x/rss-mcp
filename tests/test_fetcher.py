"""Tests for HTTP fetching, feed parsing and the SSRF guard."""

from __future__ import annotations

import pytest

from rss_mcp.config import Settings
from rss_mcp.fetcher import (
    FeedError,
    discover_feed_urls,
    fetch_document,
    parse_feed,
)
from rss_mcp.textutil import html_to_text, truncate

from .conftest import ATOM_XML, HTML_PAGE, NOT_A_FEED, RSS_XML


@pytest.fixture
def local_settings(tmp_path) -> Settings:
    return Settings(db_path=tmp_path / "unused.db", allow_private_hosts=True, http_timeout=10.0)


def test_parse_rss(local_settings: Settings) -> None:
    parsed = parse_feed(RSS_XML, local_settings)
    assert parsed.title == "Example Feed"
    assert parsed.site_url == "http://example.test/"
    assert parsed.feed_version.startswith("rss")
    assert len(parsed.entries) == 3

    first = parsed.entries[0]
    assert first.guid == "post-1"
    assert first.url == "http://example.test/posts/1"
    assert first.title == "First post"
    assert first.author == "Ada"
    assert first.published_ts == 1736157600
    # HTML flattened, script content dropped
    assert "Hello world from the first post." in (first.summary or "")
    assert "evil()" not in (first.content or "")
    assert "<p>" in (first.content or "")


def test_parse_atom(local_settings: Settings) -> None:
    parsed = parse_feed(ATOM_XML, local_settings)
    assert parsed.title == "Atom Example"
    assert parsed.description == "An Atom feed used by the tests"
    assert [entry.guid for entry in parsed.entries] == ["urn:uuid:atom-1", "urn:uuid:atom-2"]
    assert parsed.entries[1].summary == "Atom content body."
    assert parsed.entries[0].published_ts == 1736409600


def test_parse_rejects_non_feed(local_settings: Settings) -> None:
    with pytest.raises(FeedError):
        parse_feed(NOT_A_FEED, local_settings)


def test_summary_is_truncated_to_setting(tmp_path) -> None:
    settings = Settings(db_path=tmp_path / "x.db", summary_chars=80)
    body = RSS_XML.replace(b"Plain text item with no markup.", b"x" * 500)
    parsed = parse_feed(body, settings)
    assert all(len(entry.summary or "") <= 80 for entry in parsed.entries)


def test_discover_feed_urls_resolves_relative_links() -> None:
    urls = discover_feed_urls(HTML_PAGE, "http://example.test/blog/index.html")
    assert urls == ["http://example.test/rss.xml"]


def test_html_to_text_strips_markup_and_scripts() -> None:
    assert html_to_text("<p>Hello <b>world</b></p><script>x()</script>") == "Hello world"
    assert html_to_text("plain &amp; simple") == "plain & simple"
    assert html_to_text(None) == ""


def test_truncate() -> None:
    assert truncate("abcdef", 10) == ("abcdef", False)
    assert truncate("abcdef", 4) == ("a...", True)
    assert truncate(None, 10) == (None, False)


async def test_fetch_document_and_conditional_304(feed_host: str, local_settings: Settings) -> None:
    first = await fetch_document(f"{feed_host}/rss.xml", local_settings)
    assert first.status_code == 200
    assert first.etag == '"rss-v1"'
    assert first.body.startswith(b"<?xml")

    again = await fetch_document(f"{feed_host}/rss.xml", local_settings, etag=first.etag)
    assert again.not_modified is True
    assert again.body == b""


async def test_fetch_follows_redirects(feed_host: str, local_settings: Settings) -> None:
    result = await fetch_document(f"{feed_host}/redirect", local_settings)
    assert result.status_code == 200
    assert result.final_url.endswith("/rss.xml")


async def test_fetch_reports_http_errors(feed_host: str, local_settings: Settings) -> None:
    with pytest.raises(FeedError, match="404"):
        await fetch_document(f"{feed_host}/missing.xml", local_settings)


async def test_fetch_rejects_unsupported_scheme(local_settings: Settings) -> None:
    with pytest.raises(FeedError, match="scheme"):
        await fetch_document("file:///etc/passwd", local_settings)


async def test_private_hosts_blocked_by_default(feed_host: str, tmp_path) -> None:
    strict = Settings(db_path=tmp_path / "x.db", allow_private_hosts=False)
    with pytest.raises(FeedError, match="private/loopback"):
        await fetch_document(f"{feed_host}/rss.xml", strict)


async def test_download_limit_is_enforced(feed_host: str, tmp_path) -> None:
    tiny = Settings(db_path=tmp_path / "x.db", allow_private_hosts=True, max_download_bytes=1024)
    with pytest.raises(FeedError, match="download limit"):
        await fetch_document(f"{feed_host}/rss.xml", tiny)


def test_html_detection() -> None:
    from rss_mcp.fetcher import FetchResult

    assert FetchResult(200, "u", HTML_PAGE, "text/html").is_html is True
    assert FetchResult(200, "u", RSS_XML, "application/rss+xml").is_html is False
    assert FetchResult(200, "u", b"<!DOCTYPE html><html></html>", None).is_html is True
