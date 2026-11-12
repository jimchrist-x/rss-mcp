# rss-mcp

An [MCP](https://modelcontextprotocol.io) server that stores your RSS/Atom
subscriptions in a local **SQLite** file and serves them to an LLM over
**stdio**.

No accounts, no cloud, no background daemon: the client launches the server as a
subprocess, the server owns `feeds.db`, and every tool call is a plain SQLite
query plus (when you ask for it) one HTTP fetch of a feed.

```
MCP client ──stdio(JSON-RPC)──> rss-mcp ──> SQLite (feeds, entries)
                                    └─────> HTTP GET (feed XML, conditional)
```

## Features

- **SQLite persistence** -- one file, two tables (`feeds`, `entries`), WAL mode,
  `(feed_id, guid)` uniqueness so refreshing can never duplicate an item.
- **Conditional refresh** -- ETag / `If-Modified-Since` validators are stored per
  feed, so unchanged feeds answer `304` and cost almost nothing.
- **Read API over stdio** -- list, page, filter (unread/starred/since), search,
  and fetch an entry's full body; per-feed unread counters for triage.
- **HTML autodiscovery** -- give it a blog homepage; it finds
  `<link rel="alternate" type="application/rss+xml">`.
- **OPML in/out** -- migrate from any other reader and back.
- **SSRF guard** -- refuses loopback/private/link-local targets (and every
  redirect hop is re-checked) unless `RSS_MCP_ALLOW_PRIVATE_HOSTS=1`.
- **Structured output** -- every tool declares a Pydantic output schema, so
  clients get typed JSON instead of prose.
- **Bounded growth** -- keeps at most `RSS_MCP_MAX_ENTRIES_PER_FEED` (default
  500) entries per feed, never deleting starred ones.

## Requirements

Python 3.11+ (developed and tested on 3.12). Dependencies: `mcp>=2.3`,
`feedparser`, `httpx`.

## Install

```bash
cd rss-mcp
uv venv                      # or: python -m venv .venv
uv pip install -e .
```

That gives you two equivalent entry points:

```bash
rss-mcp --db ./data/feeds.db        # console script
python -m rss_mcp --db ./data/feeds.db
```

Both speak MCP on **stdio**. Logs go to stderr; stdout carries JSON-RPC only.

## Configure an MCP client

Add a server entry to the client config (Claude Desktop
`claude_desktop_config.json`, MCPO `config.json`, Cursor, ...):

```json
{
  "mcpServers": {
    "time": {
      "command": "uvx",
      "args": ["mcp-server-time", "--local-timezone=UTC"]
    },
    "rss": {
      "command": "uvx",
      "args": [
        "--from", "/home/you/rss-mcp",
        "rss-mcp",
        "--db", "/home/you/rss-mcp/data/feeds.db"
      ]
    }
  }
}
```

`uvx --from <path> rss-mcp` builds this checkout once, caches it and runs the
console script from there, so the entry stays in the same `command`/`args` shape
as any other `uvx` server (no venv path to maintain). Pointing `command` at
`/home/you/rss-mcp/.venv/bin/rss-mcp` works too and starts faster.

With [MCPO](https://github.com/open-webui/mcpo) in front of Open WebUI, the same
entry works unchanged inside `config.json`; MCPO exposes it as
`http://mcpo:8000/rss`.

## Use with MCPO / Open WebUI

MCPO proxies the stdio server as REST, one mount point per server name, so
Open WebUI registers `http://mcpo:8000/rss` and calls `POST /rss/add_feed`,
`POST /rss/list_entries`, ...

Current mcpo releases need two constraints that are easy to miss:
**`mcp<2`** (mcpo still speaks SDK v1, rss-mcp needs v2) and
**`fastapi==0.115.12`** (newer FastAPI dropped the `ssl_certfile` argument mcpo
passes). Because of the first one, mcpo and rss-mcp cannot share a venv.

```bash
# mcpo itself, once, with the pins baked in
uv tool install "mcpo==0.0.20" --with "mcp<2" --with "fastapi==0.115.12"

# run it against the config that holds the time + rss entries
mcpo --config /home/you/config.json --host 127.0.0.1 --port 8000 --api-key change-me
# or, from this repo, with sensible local defaults (PORT=8765 etc.):
./scripts/run-mcpo-local.sh

curl -s -X POST http://127.0.0.1:8000/rss/list_feeds \
  -H "Authorization: Bearer change-me" -H 'Content-Type: application/json' -d '{}'
```

For the container/compose recipe (`Dockerfile` with two venvs,
`mcpo/config.docker.json`, compose diff, Open WebUI registration and a
troubleshooting table) see **[docs/MCPO.md](docs/MCPO.md)**.

### Environment variables

| Variable | Default | Meaning |
| --- | --- | --- |
| `RSS_MCP_DB` | `~/.local/share/rss-mcp/feeds.db` (`$XDG_DATA_HOME` aware) | SQLite file. `--db` wins over this. |
| `RSS_MCP_HTTP_TIMEOUT` | `20` | Per-request timeout, seconds. |
| `RSS_MCP_USER_AGENT` | `rss-mcp/0.1 (...)` | UA string sent to feed servers. |
| `RSS_MCP_MAX_ENTRIES_PER_FEED` | `500` | Retention cap per feed (starred items are exempt). |
| `RSS_MCP_MAX_DOWNLOAD_BYTES` | `5000000` | Hard cap on a single feed download. |
| `RSS_MCP_SUMMARY_CHARS` | `600` | Length of the stored plain-text excerpt. |
| `RSS_MCP_ALLOW_PRIVATE_HOSTS` | `0` | Set `1` to allow localhost/LAN feed URLs (useful in dev). |
| `RSS_MCP_LOG_LEVEL` | `INFO` | stderr log level. |

## Tools

| Tool | What it does |
| --- | --- |
| `add_feed(url, title?, auto_discover?)` | Subscribe (or refresh if already saved) and store current items. |
| `list_feeds()` | All subscriptions with `entry_count` / `unread_count`. |
| `remove_feed(feed_id?, url?)` | Delete a feed and its entries. |
| `refresh_feed(feed_id?, url?)` | Conditional GET of one feed. |
| `refresh_all_feeds()` | Refresh everything, per-feed success/failure report. |
| `list_entries(feed_id?, limit, offset, unread_only, starred_only, since, oldest_first)` | Paged reading, newest first. |
| `get_entry(entry_id, include_content?, max_content_chars?, mark_read?)` | Full item body; marks read by default. |
| `search_entries(query, feed_id?, limit?)` | Case-insensitive search over title/summary/body/author. |
| `mark_entries(entry_ids?, feed_id?, read?, starred?)` | Flag updates, including mark-all-read for one feed. |
| `import_opml(source, limit?)` | Load subscriptions from a local file or URL. |
| `export_opml(path)` | Write OPML 2.0 with every subscription. |
| `server_info()` | Version, DB path, counters. |

Every tool returns a typed object (see `src/rss_mcp/models.py`) both as
`structuredContent` and as a pretty-printed text block, so it works with clients
that understand either.

## Resources

| URI | Content |
| --- | --- |
| `rss://feeds` | All subscriptions as JSON. |
| `rss://feeds/{feed_id}/entries` | Latest 20 stored entries for a feed. |
| `rss://entries/{entry_id}` | One entry with its body (read-only, does not mark it read). |

## Typical session

```text
add_feed("https://blog.python.org")          -> feed id 1, 20 entries stored
list_entries(unread_only=true, limit=5)      -> 5 items, total=20, has_more=true
get_entry(17)                                -> body, marked read
search_entries("sqlite")                     -> hits across all feeds
refresh_all_feeds()                          -> 1 updated (0 new), 0 failed
```

## Database

```sql
feeds(id, url UNIQUE, title, site_url, description, etag, last_modified,
      added_at, updated_at, last_fetched_at, last_error, is_active)

entries(id, feed_id -> feeds(id) ON DELETE CASCADE, guid, url, title, author,
        summary, content, published_at, published_ts, fetched_at,
        is_read, is_starred, UNIQUE(feed_id, guid))
```

Read-only views `feed_overview` and `entry_overview` add the counters and
`feed_title` joins used by the tools. Schema version lives in
`PRAGMA user_version`; the server refuses to open a file written by a newer
schema instead of corrupting it.

Inspect it any time: `sqlite3 data/feeds.db 'select id,title,url from feeds;'`

## Development

```bash
uv pip install -e . --group dev      # pytest + pytest-asyncio
.venv/bin/python -m pytest -q        # unit + stdio end-to-end tests
.venv/bin/rss-mcp --db ./data/feeds.db  # manual run (speaks MCP on stdin/stdout)
```

`scripts/stdio_client.py` is a tiny MCP client for poking the server without a
full client app:

```bash
.venv/bin/python scripts/stdio_client.py list-tools
.venv/bin/python scripts/stdio_client.py call add_feed url=https://blog.python.org
.venv/bin/python scripts/stdio_client.py call list_entries limit=5 --text
.venv/bin/python scripts/stdio_client.py read-resource rss://feeds
```

Layout:

```
rss-mcp/
├── pyproject.toml
├── README.md
├── Dockerfile        # image with mcpo + rss-mcp in separate venvs (see docs/MCPO.md)
├── mcpo/             # ready-made MCPO configs (host + docker paths)
├── docs/MCPO.md      # step-by-step MCPO / Open WebUI integration
├── src/rss_mcp/
│   ├── config.py     # env-driven Settings
│   ├── db.py         # SQLite schema + queries
│   ├── fetcher.py    # HTTP GET (redirects, SSRF guard), feedparser
│   ├── textutil.py   # HTML -> plain text, truncation
│   ├── models.py     # Pydantic tool results
│   └── server.py     # MCPServer: tools, resources, CLI
├── tests/            # pytest, incl. stdio end-to-end
└── scripts/          # ad-hoc stdio smoke client
```

The tests spin up a throwaway HTTP server that serves RSS, Atom, an HTML
autodiscovery page and a `304`-capable endpoint, then talk to the real MCP
server over a real stdio pipe -- no network access required.

## Notes & limits

- stdio is the supported transport. `--transport streamable-http` exists but is
  untested here; the tool set is identical.
- Feed titles are never overwritten by a refresh; pass `title=` to `add_feed`
  again to rename one.
- Entries are de-duplicated on their feed-provided GUID and never rewritten;
  a corrected item only updates after `remove_feed` + `add_feed`.
- Feeds that emit no dates are sorted last among their feed's entries.
- There is no scheduler: call `refresh_all_feeds` from your client or a cron job
  when you want new items.

## License

MIT
