#!/usr/bin/env bash
# Run mcpo locally against /home/user/config.json (time + rss).
#
# Port 8000 is taken on this workspace box (the session server listens there),
# so the default here is 8765. Override any of it:
#
#   PORT=8000 API_KEY=... ./scripts/run-mcpo-local.sh
#   CONFIG=/path/to/config.json ./scripts/run-mcpo-local.sh
#
# Requires: `mcpo` on PATH. If it is missing:
#   uv tool install "mcpo==0.0.20" --with "mcp<2" --with "fastapi==0.115.12"
set -euo pipefail

CONFIG="${CONFIG:-/home/user/config.json}"
PORT="${PORT:-8765}"
HOST="${HOST:-127.0.0.1}"
API_KEY="${API_KEY:-${MCPO_API_KEY:-local-dev-key}}"
LOG_LEVEL="${LOG_LEVEL:-INFO}"

command -v mcpo >/dev/null 2>&1 || {
  echo "mcpo not found on PATH. Install it with:" >&2
  echo '  uv tool install "mcpo==0.0.20" --with "mcp<2" --with "fastapi==0.115.12"' >&2
  exit 1
}

echo "mcpo: config=$CONFIG host=$HOST port=$PORT log=$LOG_LEVEL" >&2
exec mcpo --config "$CONFIG" --host "$HOST" --port "$PORT" \
          --api-key "$API_KEY" --log-level "$LOG_LEVEL"
