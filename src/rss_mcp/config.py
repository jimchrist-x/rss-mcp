"""Environment-driven configuration for the rss-mcp server.

Every knob is read from the environment once, on first use, so the server can be
reconfigured by an MCP client simply by launching it with a different ``env``
block. Nothing is read from ``sys.argv`` except in :func:`rss_mcp.server.main`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

VERSION = "0.1.0"

DEFAULT_USER_AGENT = f"rss-mcp/{VERSION} (+https://github.com/rss-mcp/rss-mcp; MCP RSS reader)"

_TRUTHY = {"1", "true", "yes", "on", "y"}
_FALSY = {"0", "false", "no", "off", "n"}


def _env_str(name: str, default: str) -> str:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    return value.strip()


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        return default
    return value if value >= minimum else default


def _env_float(name: str, default: float, *, minimum: float = 0.1) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw.strip())
    except ValueError:
        return default
    return value if value >= minimum else default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in _TRUTHY:
        return True
    if value in _FALSY:
        return False
    return default


def default_db_path() -> Path:
    """Resolve the SQLite file location.

    ``RSS_MCP_DB`` wins. Otherwise XDG data dir (``~/.local/share/rss-mcp/feeds.db``
    on Linux/macOS). For development, point ``RSS_MCP_DB`` at ``./data/feeds.db``
    so every artifact stays inside the checkout.
    """

    override = os.environ.get("RSS_MCP_DB")
    if override and override.strip():
        return Path(override.strip()).expanduser()
    data_home = os.environ.get("XDG_DATA_HOME")
    base = (
        Path(data_home).expanduser()
        if data_home and data_home.strip()
        else (Path.home() / ".local" / "share")
    )
    return base / "rss-mcp" / "feeds.db"


@dataclass(frozen=True, slots=True)
class Settings:
    """Runtime settings for a single server process."""

    db_path: Path
    http_timeout: float = 20.0
    user_agent: str = DEFAULT_USER_AGENT
    max_entries_per_feed: int = 500
    max_download_bytes: int = 5_000_000
    allow_private_hosts: bool = False
    summary_chars: int = 600

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            db_path=default_db_path(),
            http_timeout=_env_float("RSS_MCP_HTTP_TIMEOUT", 20.0),
            user_agent=_env_str("RSS_MCP_USER_AGENT", DEFAULT_USER_AGENT),
            max_entries_per_feed=_env_int("RSS_MCP_MAX_ENTRIES_PER_FEED", 500),
            max_download_bytes=_env_int("RSS_MCP_MAX_DOWNLOAD_BYTES", 5_000_000, minimum=1024),
            allow_private_hosts=_env_bool("RSS_MCP_ALLOW_PRIVATE_HOSTS", False),
            summary_chars=_env_int("RSS_MCP_SUMMARY_CHARS", 600, minimum=80),
        )
