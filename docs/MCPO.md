# Running rss-mcp behind MCPO

[MCPO](https://github.com/open-webui/mcpo) turns an MCP stdio server into an
OpenAPI/REST service that Open WebUI (or anything that speaks OpenAPI) can call.

The goal here is a `config.json` entry that looks exactly like the existing
`time` one — `command` + `args`, nothing exotic:

```json
"rss": {
  "command": "uvx",
  "args": ["--from", "/home/user/rss-mcp", "rss-mcp", "--db", "/home/user/rss-mcp/data/feeds.db"]
}
```

`uvx --from <path> <script>` builds the local checkout once, caches it, and runs
the `rss-mcp` console script from that cache — so no venv path leaks into the
config and updates to the source are picked up on the next start.

Everything below was executed on this machine. The Docker path was validated by
replaying the same install/run steps in plain virtualenvs (no Docker daemon is
available here, so `docker build` itself is unverified).

---

## 0. Two pins you need (read this first)

mcpo 0.0.20 declares `mcp>=1.17.0` and `fastapi>=0.115.12` with no upper bound,
and both moved in ways that break it:

| Symptom | Cause | Fix |
| --- | --- | --- |
| `ImportError: cannot import name 'streamablehttp_client' from 'mcp.client.streamable_http'` | mcpo is written against MCP Python SDK **v1**; PyPI now resolves **v2** | install mcpo with `"mcp<2"` |
| `TypeError: FastAPI.__init__() got an unexpected keyword argument 'ssl_certfile'` | newer FastAPI dropped that constructor kwarg, mcpo still passes it | install mcpo with `"fastapi==0.115.12"` |

Install mcpo once, with the pins baked in (this is what puts `mcpo` on `PATH`):

```bash
uv tool install "mcpo==0.0.20" --with "mcp<2" --with "fastapi==0.115.12"
mcpo --help | head -5
```

Without `uv tool install` you can always run it ad hoc:

```bash
uvx --with "mcp<2" --with "fastapi==0.115.12" mcpo --config config.json --port 8000
```

Note that **rss-mcp requires `mcp>=2.3`**, so it can never share a site-packages
tree with mcpo. That is why the config uses `uvx` (its own cached environment)
instead of installing rss-mcp into mcpo's environment.

---

## 1. Local config + run (verified)

`/home/user/config.json` — the `time` entry is untouched, `rss` follows the same
shape:

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
        "--from", "/home/user/rss-mcp",
        "rss-mcp",
        "--db", "/home/user/rss-mcp/data/feeds.db"
      ]
    }
  }
}
```

Start it:

```bash
mcpo --config /home/user/config.json --host 127.0.0.1 --port 8765 --api-key local-dev-key
```

or, from the repo, `./scripts/run-mcpo-local.sh` (same thing; `PORT`, `API_KEY`,
`CONFIG`, `LOG_LEVEL` are overridable environment variables).

> **Port 8000 is taken on this workspace box** — the session server listens
> there, so a local mcpo must use another port (the helper script defaults to
> `8765`). Inside the compose stack nothing changes: mcpo stays on `8000` and is
> only reachable as `http://mcpo:8000`.

Expected startup lines (both servers connect):

```
INFO - Initiating connection for server: 'time'...
INFO - Successfully connected to 'time'.
INFO - Initiating connection for server: 'rss'...
INFO rss_mcp: rss-mcp 0.1.0 ready on stdio (db=/home/user/rss-mcp/data/feeds.db, feeds=1, entries=50)
INFO - Successfully connected to 'rss'.
INFO - Successfully connected to:
INFO -   - time
INFO -   - rss
INFO:     Uvicorn running on http://127.0.0.1:8765 (Press CTRL+C to quit)
```

First launch of `rss-mcp` through `uvx` builds the project (~10 s). Pre-warm it
so mcpo's connect timeout is never a factor:

```bash
uvx --from /home/user/rss-mcp rss-mcp --version   # caches the environment
```

### Verify it

```bash
API=http://127.0.0.1:8765
KEY=local-dev-key

# tool paths live under /<server-name>, NOT at the root
curl -s -H "Authorization: Bearer $KEY" $API/rss/openapi.json \
  | python3 -c "import json,sys; print(list(json.load(sys.stdin)['paths']))"
# ['/add_feed', '/list_feeds', '/remove_feed', '/refresh_feed', '/refresh_all_feeds',
#  '/list_entries', '/get_entry', '/search_entries', '/mark_entries',
#  '/import_opml', '/export_opml', '/server_info']

# root openapi.json is EMPTY in --config mode; that is expected
curl -s $API/openapi.json | python3 -c "import json,sys; print(json.load(sys.stdin)['paths'])"
# []

# the pre-existing time server still works
curl -s -X POST $API/time/get_current_time -H "Authorization: Bearer $KEY" \
     -H 'Content-Type: application/json' -d '{"timezone":"Europe/Athens"}'
# {"timezone":"Europe/Athens","datetime":"2026-11-12T10:40:04+02:00","day_of_week":"Thursday","is_dst":false}

# rss tools: POST + JSON body (the body is the tool's arguments)
curl -s -X POST $API/rss/add_feed -H "Authorization: Bearer $KEY" \
     -H 'Content-Type: application/json' -d '{"url":"https://blog.python.org"}'
# {"ok":true,"message":"Subscribed to Python Insider and stored 50 entries ..."}

curl -s -X POST $API/rss/list_feeds -H "Authorization: Bearer $KEY" \
     -H 'Content-Type: application/json' -d '{}'
# {"feeds":[...],"total":1,"database":"/home/user/rss-mcp/data/feeds.db"}

curl -s -X POST $API/rss/list_entries -H "Authorization: Bearer $KEY" \
     -H 'Content-Type: application/json' -d '{"limit":3,"unread_only":true}'

# 401 without the key
curl -s -o /dev/null -w '%{http_code}\n' -X POST $API/rss/list_feeds \
     -H 'Content-Type: application/json' -d '{}'
```

Because rss-mcp returns Pydantic models, mcpo hands Open WebUI clean JSON
(`structuredContent`) instead of a stringified blob — no extra parsing needed.

### Alternative entry styles

Same server, three ways to spell `command` — pick per environment:

```json
// 1. uvx from the local checkout (shown above, no absolute venv paths)
{ "command": "uvx", "args": ["--from", "/home/user/rss-mcp", "rss-mcp", "--db", "/home/user/rss-mcp/data/feeds.db"] }

// 2. the project venv directly (fastest start, no uv resolution)
{ "command": "/home/user/rss-mcp/.venv/bin/rss-mcp", "args": ["--db", "/home/user/rss-mcp/data/feeds.db"] }

// 3. inside the container image built from this repo
{ "command": "/opt/rss-venv/bin/rss-mcp", "args": ["--db", "/var/lib/rss-mcp/feeds.db"] }
```

Options for `args` (all optional, all have env-var equivalents):

```
--db PATH            SQLite file (default ~/.local/share/rss-mcp/feeds.db)
--log-level LEVEL    DEBUG|INFO|WARNING|ERROR|CRITICAL (stderr)
--transport stdio    default; stdio is what MCPO needs
```

An `"env": { ... }` block also works — mcpo merges it over the parent
environment, e.g. `{"RSS_MCP_ALLOW_PRIVATE_HOSTS": "1"}` to allow localhost
feeds.

### Register it in Open WebUI

`Settings → Admin → Integrations → Tools → Add connection`:

* **URL**: `http://127.0.0.1:8765/rss` when mcpo runs on the host, or
  `http://mcpo:8000/rss` when it runs in the compose stack
* **API key**: the value of `--api-key` (`MCPO_API_KEY` in compose)

Do **not** register the root URL — in `--config` mode it exposes no tool paths.

---

## 2. Option B — Docker / docker-compose (matches your existing MCPO stack)

The repository ships a `Dockerfile` that builds one image with **two** venvs:
`/opt/mcpo-venv` (mcpo + `mcp<2` + `fastapi==0.115.12` + `uv` for other
`uvx`-based servers) and `/opt/rss-venv` (rss-mcp on `mcp>=2.3`).

```bash
cd /home/user
docker build -t rss-mcp-mcpo:0.1.0 ./rss-mcp
```

### 2a. Merge the server into your `config.json`

`/home/user/config.json` currently holds `time` and `rss`; inside the image the
console script is `/opt/rss-venv/bin/rss-mcp` and the database lives in the
mounted volume:

```json
{
  "mcpServers": {
    "time": {
      "command": "uvx",
      "args": ["mcp-server-time", "--local-timezone=UTC"]
    },
    "rss": {
      "command": "/opt/rss-venv/bin/rss-mcp",
      "args": ["--db", "/var/lib/rss-mcp/feeds.db", "--log-level", "INFO"],
      "env": { "RSS_MCP_ALLOW_PRIVATE_HOSTS": "0" }
    }
  }
}
```

(`mcpo/config.docker.json` is a ready-made copy of just the `rss` block.)

> The `time` entry keeps working because the image also installs `uv`, so `uvx`
> exists inside the container.

### 2b. Compose diff for your `docker-compose-mcpo.yml`

Only the `mcpo` service changes: build instead of pulling the ghcr image, keep
the same flags/network/healthcheck, and add the persistence volume.

```yaml
services:
  mcpo:
    # was: image: ghcr.io/open-webui/mcpo:main
    build:
      context: ./rss-mcp
      dockerfile: Dockerfile
    image: rss-mcp-mcpo:0.1.0
    container_name: mcpo
    restart: unless-stopped
    command:
      - "--host"
      - "0.0.0.0"
      - "--config"
      - "/app/config.json"
      - "--port"
      - "8000"
      - "--api-key"
      - "${MCPO_API_KEY:?MCPO_API_KEY missing - copy .env.example to .env}"
    environment:
      LOG_LEVEL: "info"
    env_file:
      - .env
    volumes:
      - ./config.json:/app/config.json:ro
      - ./rss-mcp/data:/var/lib/rss-mcp      # feeds.db lives here; inspect it with sqlite3
    expose:
      - "8000"
    networks:
      - proxy
    healthcheck:
      test: ["CMD", "curl", "-fsS", "http://localhost:8000/openapi.json"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 60s
```

Then:

```bash
docker compose -f docker-compose-mcpo.yml up -d --build
docker compose -f docker-compose-mcpo.yml logs -f mcpo | grep -E "rss|Startup Summary"
```

In Open WebUI register `http://mcpo:8000/rss` with `MCPO_API_KEY` — identical to
Option A, because MCPO's surface does not change with the packaging.

### Variant: keep the upstream ghcr image

If you would rather not build, install rss-mcp into a side venv at container
start (the image is Python-based, so `python -m venv` is available), mounting
the project read-only:

```yaml
    image: ghcr.io/open-webui/mcpo:main
    volumes:
      - ./config.json:/app/config.json:ro
      - ./rss-mcp:/opt/rss-mcp:ro
      - ./rss-mcp/data:/var/lib/rss-mcp
    entrypoint: ["/bin/sh", "-c"]
    command:
      - "python -m venv /opt/rss-venv && /opt/rss-venv/bin/pip install /opt/rss-mcp && exec mcpo --host 0.0.0.0 --port 8000 --config /app/config.json --api-key \"$$MCPO_API_KEY\""
```

This needs network access at start-up and is slower to boot; the built image is
reproducible and starts instantly. (Not executed here — no Docker daemon.)

---

## 3. What the model sees

| MCP tool | REST endpoint | Example body |
| --- | --- | --- |
| `add_feed` | `POST /rss/add_feed` | `{"url":"https://blog.python.org"}` |
| `list_feeds` | `POST /rss/list_feeds` | `{}` |
| `remove_feed` | `POST /rss/remove_feed` | `{"feed_id":1}` |
| `refresh_feed` | `POST /rss/refresh_feed` | `{"url":"https://blog.python.org/rss.xml"}` |
| `refresh_all_feeds` | `POST /rss/refresh_all_feeds` | `{}` |
| `list_entries` | `POST /rss/list_entries` | `{"limit":5,"unread_only":true}` |
| `get_entry` | `POST /rss/get_entry` | `{"entry_id":12,"max_content_chars":1500}` |
| `search_entries` | `POST /rss/search_entries` | `{"query":"sqlite"}` |
| `mark_entries` | `POST /rss/mark_entries` | `{"feed_id":1,"read":true}` |
| `import_opml` | `POST /rss/import_opml` | `{"source":"/var/lib/rss-mcp/feeds.opml"}` |
| `export_opml` | `POST /rss/export_opml` | `{"path":"/var/lib/rss-mcp/feeds.opml"}` |
| `server_info` | `POST /rss/server_info` | `{}` |

MCP **resources** (`rss://feeds`, `rss://feeds/{id}/entries`,
`rss://entries/{id}`) are not proxied by MCPO — it only exposes tools. Use the
tools, or read the SQLite file directly.

Paths inside `import_opml` / `export_opml` are resolved by the server process, so
in the container use the volume path (`/var/lib/rss-mcp/...`), not a host path.

---

## 4. Troubleshooting

| Symptom | Fix |
| --- | --- |
| mcpo crashes at import with `streamablehttp_client` | add `--with "mcp<2"` (host) or the pinned image/venv |
| mcpo crashes with `ssl_certfile` | add `--with "fastapi==0.115.12"` |
| `pip` says mcpo and rss-mcp conflict | expected: mcpo needs SDK v1, rss-mcp v2 — keep separate envs (Dockerfile does this) |
| `[Errno 98] address already in use` | port 8000 is busy on this box; use `--port 8765` locally |
| `rss` fails to connect on first ever run | pre-warm: `uvx --from /home/user/rss-mcp rss-mcp --version` |
| Tool endpoints 401 | send `Authorization: Bearer <api-key>`; add `--strict-auth` only if you also want the docs protected |
| `/openapi.json` has no tools | use `/<server-name>/openapi.json` |
| `import_opml` cannot find the file | paths are resolved inside the server's environment (`/var/lib/rss-mcp/...` in Docker) |
| `Permission denied` on `feeds.db` | the volume must be writable by the container user |
| Feeds on localhost/LAN rejected | `"env": {"RSS_MCP_ALLOW_PRIVATE_HOSTS": "1"}` plus `extra_hosts: host.docker.internal:host-gateway` |
| Config edit not picked up | mcpo reads the config at start (no hot reload unless `--hot-reload`); restart it |
