"""Pydantic models returned by the MCP tools.

Returning explicit models (instead of loose dicts) gives MCP clients a real
``outputSchema`` and gives the calling model stable field names to reason about.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class FeedInfo(BaseModel):
    """A saved subscription."""

    id: int = Field(description="Stable feed id used by the other tools")
    url: str = Field(description="Feed URL as stored (the URL that was fetched)")
    title: str | None = Field(default=None, description="Feed title, if the feed declares one")
    site_url: str | None = Field(default=None, description="Homepage of the site behind the feed")
    description: str | None = Field(default=None, description="Feed subtitle/description")
    is_active: bool = Field(default=True, description="Whether refreshes are attempted")
    added_at: str = Field(description="When the feed was subscribed (UTC, ISO-8601)")
    last_fetched_at: str | None = Field(
        default=None, description="Last successful/failed fetch (UTC)"
    )
    last_error: str | None = Field(default=None, description="Last fetch error, cleared on success")
    entry_count: int = Field(default=0, description="Entries currently stored for this feed")
    unread_count: int = Field(default=0, description="Stored entries not yet marked read")


class FeedList(BaseModel):
    feeds: list[FeedInfo] = Field(default_factory=list, description="Saved feeds, sorted by title")
    total: int = Field(default=0, description="Number of feeds returned")
    database: str = Field(description="Absolute path of the SQLite file backing this server")


class EntryInfo(BaseModel):
    """A feed item without its full body."""

    id: int = Field(description="Entry id used by get_entry / mark_entries")
    feed_id: int = Field(description="Owning feed id")
    feed_title: str | None = Field(default=None, description="Owning feed title")
    title: str | None = Field(default=None, description="Item title")
    url: str | None = Field(default=None, description="Link to the item")
    author: str | None = Field(default=None, description="Item author, when provided")
    published_at: str | None = Field(default=None, description="Publication time (UTC, ISO-8601)")
    summary: str | None = Field(default=None, description="Plain-text excerpt")
    is_read: bool = Field(default=False, description="Read flag")
    is_starred: bool = Field(default=False, description="Starred flag")


class EntryDetail(EntryInfo):
    """A single entry, optionally with its full body."""

    content: str | None = Field(default=None, description="Raw item body (HTML or text)")
    content_truncated: bool = Field(
        default=False, description="True when content was cut to max_content_chars"
    )
    guid: str = Field(description="Feed-provided unique id used for de-duplication")
    fetched_at: str = Field(description="When this entry was first stored (UTC, ISO-8601)")


class EntryList(BaseModel):
    entries: list[EntryInfo] = Field(default_factory=list)
    returned: int = Field(default=0, description="Entries in this page")
    total: int = Field(default=0, description="Total entries matching the filters")
    offset: int = Field(default=0)
    has_more: bool = Field(default=False, description="True when offset + returned < total")


class RefreshReport(BaseModel):
    """Result of refreshing a single feed."""

    feed_id: int = Field(description="Feed id, or -1 when the feed could not be resolved")
    url: str = Field(description="Feed URL that was refreshed")
    title: str | None = Field(default=None)
    status: str = Field(description="One of: updated, not_modified, failed")
    new_entries: int = Field(default=0, description="Entries added by this refresh")
    http_status: int | None = Field(default=None, description="HTTP status of the fetch")
    error: str | None = Field(default=None, description="Error message when status is failed")


class RefreshSummary(BaseModel):
    results: list[RefreshReport] = Field(default_factory=list)
    refreshed: int = Field(default=0, description="Feeds refreshed successfully (incl. 304)")
    failed: int = Field(default=0, description="Feeds that failed")
    new_entries: int = Field(default=0, description="Total new entries across all feeds")


class FeedActionResult(BaseModel):
    """Outcome of add/remove/refresh operations on one feed."""

    ok: bool
    message: str = Field(description="Human-readable summary of what happened")
    feed: FeedInfo | None = Field(default=None, description="State of the feed after the call")
    new_entries: int = Field(default=0, description="Entries inserted by this call")
    removed_entries: int = Field(default=0, description="Entries deleted together with the feed")
    discovered_from: str | None = Field(
        default=None, description="HTML page the feed URL was auto-discovered on, if any"
    )


class EntryMutation(BaseModel):
    ok: bool
    message: str
    affected: int = Field(default=0, description="Number of entries updated")


class OpmlImportResult(BaseModel):
    ok: bool
    message: str
    imported: int = Field(default=0, description="Feeds newly added")
    skipped: int = Field(default=0, description="Feeds already stored")
    failed: list[str] = Field(default_factory=list, description="URLs that could not be added")


class OpmlExportResult(BaseModel):
    ok: bool
    message: str
    path: str = Field(description="Where the OPML document was written")
    feeds: int = Field(default=0, description="Feeds written to the document")


class ServerInfo(BaseModel):
    name: str
    version: str
    database: str
    transport: str
    feeds: int
    entries: int
    unread: int
    starred: int
