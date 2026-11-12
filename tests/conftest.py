"""Shared fixtures: a throwaway feed host and an isolated SQLite file.

The HTTP server below is the only "network" the tests touch -- everything runs
on 127.0.0.1, which is why the fixture sets ``RSS_MCP_ALLOW_PRIVATE_HOSTS=1``.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

RSS_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
  <channel>
    <title>Example Feed</title>
    <link>http://example.test/</link>
    <description>Notes about &lt;b&gt;nothing&lt;/b&gt; in particular</description>
    <item>
      <title>First post</title>
      <link>http://example.test/posts/1</link>
      <guid isPermaLink="false">post-1</guid>
      <pubDate>Mon, 06 Jan 2025 10:00:00 GMT</pubDate>
      <author>ada@example.test (Ada)</author>
      <content:encoded><![CDATA[
        <p>Hello <b>world</b> from the first post.</p><script>evil()</script>
      ]]></content:encoded>
    </item>
    <item>
      <title>Second post about SQLite</title>
      <link>http://example.test/posts/2</link>
      <guid isPermaLink="false">post-2</guid>
      <pubDate>Tue, 07 Jan 2025 11:30:00 GMT</pubDate>
      <description><![CDATA[<p>SQLite is everywhere.</p>]]></description>
    </item>
    <item>
      <title>Third post</title>
      <link>http://example.test/posts/3</link>
      <guid isPermaLink="false">post-3</guid>
      <pubDate>Wed, 08 Jan 2025 09:15:00 GMT</pubDate>
      <description>Plain text item with no markup.</description>
    </item>
  </channel>
</rss>
"""

ATOM_XML = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Atom Example</title>
  <link href="http://example.test/atom/"/>
  <subtitle>An Atom feed used by the tests</subtitle>
  <updated>2025-01-09T08:00:00Z</updated>
  <entry>
    <title>Atom item one</title>
    <id>urn:uuid:atom-1</id>
    <link href="http://example.test/atom/1"/>
    <updated>2025-01-09T08:00:00Z</updated>
    <summary>Atom summary text.</summary>
  </entry>
  <entry>
    <title>Atom item two</title>
    <id>urn:uuid:atom-2</id>
    <link href="http://example.test/atom/2"/>
    <updated>2025-01-10T08:00:00Z</updated>
    <content type="html">&lt;p&gt;Atom content body.&lt;/p&gt;</content>
  </entry>
</feed>
"""

HTML_PAGE = b"""<!doctype html>
<html><head>
  <title>Example blog</title>
  <link rel="alternate" type="application/rss+xml" title="RSS" href="/rss.xml">
</head><body><h1>Example blog</h1></body></html>
"""

HTML_NO_FEED = b"""<!doctype html>
<html><head><title>No feed here</title></head><body><p>nothing</p></body></html>
"""

NOT_A_FEED = b"""<?xml version="1.0"?><note><to>you</to><body>hi</body></note>"""


class _Handler(BaseHTTPRequestHandler):
    server_version = "TestFeedHost/1.0"

    def log_message(self, format: str, *args: object) -> None:
        pass

    # -- helpers ---------------------------------------------------------
    def _send(self, status: int, body: bytes, content_type: str, etag: str | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if etag:
            self.send_header("ETag", etag)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _conditional(self, etag: str) -> bool:
        return self.headers.get("If-None-Match") == etag

    # -- routes ----------------------------------------------------------
    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        path = self.path.split("?")[0]

        if path == "/rss.xml":
            etag = '"rss-v1"'
            if self._conditional(etag):
                self._send(304, b"", "application/rss+xml", etag)
            else:
                self._send(200, RSS_XML, "application/rss+xml; charset=utf-8", etag)
        elif path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/rss.xml")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif path == "/atom.xml":
            self._send(200, ATOM_XML, "application/atom+xml; charset=utf-8")
        elif path in {"/blog/", "/blog/index.html", "/"}:
            self._send(200, HTML_PAGE, "text/html; charset=utf-8")
        elif path == "/no-feed":
            self._send(200, HTML_NO_FEED, "text/html; charset=utf-8")
        elif path == "/not-a-feed.xml":
            self._send(200, NOT_A_FEED, "application/xml; charset=utf-8")
        elif path == "/missing.xml":
            self._send(404, b"gone", "text/plain")
        else:
            self._send(404, b"not found", "text/plain")


@pytest.fixture(scope="session")
def feed_host() -> Iterator[str]:
    """Base URL (``http://127.0.0.1:port``) of the local test feed host."""

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point the server at a per-test database and reset cached runtime state."""

    path = tmp_path / "feeds.db"
    monkeypatch.setenv("RSS_MCP_DB", str(path))
    monkeypatch.setenv("RSS_MCP_ALLOW_PRIVATE_HOSTS", "1")
    monkeypatch.setenv("RSS_MCP_LOG_LEVEL", "WARNING")

    from rss_mcp import server

    server.reset_runtime()
    try:
        yield path
    finally:
        server.reset_runtime()


@pytest.fixture
def settings(db_path: Path):
    from rss_mcp.config import Settings

    return Settings.from_env()


@pytest.fixture
def db(db_path: Path):
    from rss_mcp.db import Database

    database = Database(db_path)
    database.initialize()
    return database
