# rss-mcp behind MCPO (MCP -> OpenAPI -> Open WebUI)
#
# Two virtualenvs on purpose: mcpo 0.0.20 still targets MCP Python SDK v1
# (`mcp<2`), while rss-mcp needs v2 (`mcp>=2.3`). They cannot share a
# site-packages tree, so mcpo lives in /opt/mcpo-venv and rss-mcp in
# /opt/rss-venv. The MCPO config points at /opt/rss-venv/bin/rss-mcp.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PATH="/opt/mcpo-venv/bin:$PATH"

# curl: container healthcheck. (No other system packages are needed.)
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

# 1) rss-mcp itself, isolated in its own venv (mcp>=2.3).
COPY . /opt/rss-mcp
RUN python -m venv /opt/rss-venv \
 && /opt/rss-venv/bin/pip install --upgrade pip \
 && /opt/rss-venv/bin/pip install /opt/rss-mcp

# 2) mcpo, pinned to the set that is known to work together. `uv` is installed
#    as well so existing uvx-based servers in the same config keep working.
RUN python -m venv /opt/mcpo-venv \
 && /opt/mcpo-venv/bin/pip install --upgrade pip \
 && /opt/mcpo-venv/bin/pip install \
      "mcpo==0.0.20" \
      "mcp<2" \
      "fastapi==0.115.12" \
      "uv"

# Writable home for feeds.db -- mount a volume here to keep subscriptions.
RUN mkdir -p /var/lib/rss-mcp
VOLUME ["/var/lib/rss-mcp"]

EXPOSE 8000
ENTRYPOINT ["mcpo"]
CMD ["--host", "0.0.0.0", "--port", "8000", "--config", "/app/config.json"]
