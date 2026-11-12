"""Settings / CLI plumbing tests."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from rss_mcp.config import Settings, default_db_path


def test_default_db_path_honours_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("RSS_MCP_DB", str(tmp_path / "custom.db"))
    assert default_db_path() == tmp_path / "custom.db"


def test_default_db_path_uses_xdg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("RSS_MCP_DB", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert default_db_path() == tmp_path / "rss-mcp" / "feeds.db"


def test_settings_from_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("RSS_MCP_DB", str(tmp_path / "x.db"))
    monkeypatch.setenv("RSS_MCP_HTTP_TIMEOUT", "5.5")
    monkeypatch.setenv("RSS_MCP_MAX_ENTRIES_PER_FEED", "42")
    monkeypatch.setenv("RSS_MCP_ALLOW_PRIVATE_HOSTS", "yes")
    settings = Settings.from_env()
    assert settings.http_timeout == 5.5
    assert settings.max_entries_per_feed == 42
    assert settings.allow_private_hosts is True


def test_settings_fall_back_on_garbage(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("RSS_MCP_DB", str(tmp_path / "x.db"))
    monkeypatch.setenv("RSS_MCP_HTTP_TIMEOUT", "not-a-number")
    monkeypatch.setenv("RSS_MCP_MAX_ENTRIES_PER_FEED", "-3")
    monkeypatch.setenv("RSS_MCP_ALLOW_PRIVATE_HOSTS", "maybe")
    settings = Settings.from_env()
    assert settings.http_timeout == 20.0
    assert settings.max_entries_per_feed == 500
    assert settings.allow_private_hosts is False


def test_cli_db_flag_sets_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys) -> None:
    from rss_mcp import server

    target = tmp_path / "cli.db"
    monkeypatch.delenv("RSS_MCP_DB", raising=False)
    server.reset_runtime()

    def fake_run(transport: str = "stdio", **kwargs: object) -> None:
        return None

    monkeypatch.setattr(server.mcp, "run", fake_run)
    monkeypatch.setattr(server, "_configure_logging", lambda level: None)
    try:
        assert server.main(["--db", str(target)]) == 0
        assert os.environ["RSS_MCP_DB"] == str(target)
        assert target.exists()
    finally:
        server.reset_runtime()


def test_cli_rejects_unknown_transport() -> None:
    from rss_mcp import server

    with pytest.raises(SystemExit):
        server.main(["--transport", "carrier-pigeon"])
