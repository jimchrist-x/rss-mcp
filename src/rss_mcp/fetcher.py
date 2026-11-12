"""HTTP fetching and feed parsing.

Kept separate from the MCP layer so it can be unit tested without a client, and
so the server module only deals with "what does the tool return".
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import feedparser
import httpx

from .config import Settings
from .db import NewEntry, struct_to_timestamp
from .textutil import html_to_text, normalise_whitespace, truncate

MAX_REDIRECTS = 5

ACCEPT_HEADER = (
    "application/rss+xml, application/atom+xml, application/feed+json, "
    "application/xml;q=0.9, text/xml;q=0.8, text/html;q=0.5, */*;q=0.1"
)

_FEED_MIME_HINTS = (
    "application/rss",
    "application/atom",
    "application/xml",
    "application/rdf",
    "text/xml",
    "application/feed+json",
)


class FeedError(Exception):
    """An expected failure: bad URL, unreachable host, unparsable document."""


@dataclass(slots=True)
class FetchResult:
    """Outcome of a single (possibly redirected) HTTP GET."""

    status_code: int
    final_url: str
    body: bytes = b""
    content_type: str | None = None
    etag: str | None = None
    last_modified: str | None = None

    @property
    def not_modified(self) -> bool:
        return self.status_code == 304

    @property
    def is_html(self) -> bool:
        content_type = (self.content_type or "").lower()
        if "html" in content_type:
            return True
        head = self.body[:512].lstrip().lower()
        return head.startswith((b"<!doctype html", b"<html"))


@dataclass(slots=True)
class ParsedFeed:
    title: str | None
    site_url: str | None
    description: str | None
    entries: list[NewEntry] = field(default_factory=list)
    feed_version: str | None = None


class _FeedLinkParser(HTMLParser):
    """Collect ``<link rel="alternate">`` feed URLs from an HTML document."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.candidates: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "link":
            return
        values = {key.lower(): (value or "") for key, value in attrs}
        rel = values.get("rel", "").lower()
        mime = values.get("type", "").lower()
        href = values.get("href", "").strip()
        if not href or "alternate" not in rel:
            return
        if any(hint in mime for hint in _FEED_MIME_HINTS):
            self.candidates.append(href)


def discover_feed_urls(body: bytes, base_url: str) -> list[str]:
    """Return absolute feed URLs advertised by an HTML page (in document order)."""

    parser = _FeedLinkParser()
    try:
        parser.feed(body.decode("utf-8", errors="replace"))
        parser.close()
    except Exception:  # noqa: BLE001 - HTMLParser is best-effort
        return []
    seen: set[str] = set()
    urls: list[str] = []
    for candidate in parser.candidates:
        absolute = urljoin(base_url, candidate)
        if absolute not in seen:
            seen.add(absolute)
            urls.append(absolute)
    return urls


def _validate_url(url: str) -> str:
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"}:
        raise FeedError(f"Unsupported URL scheme {parsed.scheme!r}; use http or https")
    if not parsed.hostname:
        raise FeedError(f"URL {url!r} has no host")
    return url.strip()


async def _ensure_public_url(url: str, settings: Settings) -> None:
    """Reject loopback/private/link-local targets unless explicitly allowed."""

    if settings.allow_private_hosts:
        return
    host = urlparse(url).hostname
    if not host:
        raise FeedError(f"URL {url!r} has no host")
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise FeedError(f"Could not resolve host {host!r}: {exc}") from exc
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_reserved
            or address.is_multicast
            or address.is_unspecified
        ):
            raise FeedError(
                f"Refusing to fetch {url}: {address} is a private/loopback address. "
                "Set RSS_MCP_ALLOW_PRIVATE_HOSTS=1 to allow local feeds."
            )


async def fetch_document(
    url: str,
    settings: Settings,
    *,
    etag: str | None = None,
    last_modified: str | None = None,
) -> FetchResult:
    """GET a URL, following redirects manually so every hop passes the safety check."""

    current = _validate_url(url)
    headers = {"User-Agent": settings.user_agent, "Accept": ACCEPT_HEADER}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified

    async with httpx.AsyncClient(
        timeout=settings.http_timeout,
        follow_redirects=False,
        headers={"User-Agent": settings.user_agent},
    ) as client:
        for _hop in range(MAX_REDIRECTS + 1):
            await _ensure_public_url(current, settings)
            try:
                async with client.stream("GET", current, headers=headers) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location:
                            raise FeedError(
                                f"{current} answered {response.status_code} "
                                "without a Location header"
                            )
                        current = urljoin(str(response.url), location)
                        continue
                    if response.status_code >= 400:
                        raise FeedError(
                            f"HTTP {response.status_code} {response.reason_phrase} for {current}"
                        )
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > settings.max_download_bytes:
                            raise FeedError(
                                f"{current} is larger than the {settings.max_download_bytes} byte "
                                "download limit (RSS_MCP_MAX_DOWNLOAD_BYTES)"
                            )
                        chunks.append(chunk)
                    return FetchResult(
                        status_code=response.status_code,
                        final_url=str(response.url),
                        body=b"".join(chunks),
                        content_type=response.headers.get("content-type"),
                        etag=response.headers.get("etag"),
                        last_modified=response.headers.get("last-modified"),
                    )
            except httpx.HTTPError as exc:
                raise FeedError(f"{type(exc).__name__} while fetching {current}: {exc}") from exc
    raise FeedError(f"Too many redirects (>{MAX_REDIRECTS}) starting at {url}")


def _entry_author(entry: dict) -> str | None:
    """Prefer the parsed display name (RSS ``author`` can be ``mail (Name)``)."""

    detail = entry.get("author_detail")
    if isinstance(detail, dict):
        candidate = detail.get("name") or detail.get("email")
        cleaned = normalise_whitespace(candidate or "")
        if cleaned:
            return cleaned
    cleaned = normalise_whitespace(entry.get("author") or entry.get("dc_creator") or "")
    return cleaned or None


def _entry_guid(entry: dict, index: int) -> str:
    for key in ("id", "guid", "link"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    title = normalise_whitespace(entry.get("title") or "")
    published = entry.get("published") or entry.get("updated") or ""
    if title or published:
        return f"{title}|{published}"
    return f"urn:rss-mcp:index:{index}"


def _entry_content(entry: dict) -> str | None:
    blocks = entry.get("content")
    if isinstance(blocks, list):
        for block in blocks:
            value = block.get("value") if isinstance(block, dict) else None
            if isinstance(value, str) and value.strip():
                return value
    summary = entry.get("summary")
    if isinstance(summary, str) and summary.strip():
        return summary
    return None


def parse_feed(body: bytes, settings: Settings) -> ParsedFeed:
    """Parse RSS/RDF/Atom bytes into a :class:`ParsedFeed`."""

    parsed = feedparser.parse(body)
    if parsed.get("bozo") and not parsed.entries:
        reason = parsed.get("bozo_exception")
        raise FeedError(f"Could not parse feed: {reason or 'malformed XML'}")
    if not parsed.entries and not parsed.feed.get("title") and not parsed.get("version"):
        raise FeedError("The response is not an RSS/Atom feed (no feed metadata, no entries)")

    feed_data = parsed.feed
    title = normalise_whitespace(feed_data.get("title") or "") or None
    site_url = feed_data.get("link") or None
    description = (
        truncate(html_to_text(feed_data.get("subtitle") or feed_data.get("description")), 500)[0]
        or None
    )

    entries: list[NewEntry] = []
    for index, entry in enumerate(parsed.entries):
        content = _entry_content(entry)
        summary_text = html_to_text(content)
        entries.append(
            NewEntry(
                guid=_entry_guid(entry, index),
                url=(entry.get("link") or None),
                title=normalise_whitespace(entry.get("title") or "") or None,
                author=_entry_author(entry),
                summary=truncate(summary_text, settings.summary_chars)[0],
                content=content,
                published_ts=struct_to_timestamp(
                    entry.get("published_parsed") or entry.get("updated_parsed")
                ),
            )
        )
    return ParsedFeed(
        title=title,
        site_url=site_url,
        description=description,
        entries=entries,
        feed_version=parsed.get("version"),
    )
