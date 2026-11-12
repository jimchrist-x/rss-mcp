#!/usr/bin/env python
"""Minimal stdio MCP client for manual smoke tests.

Examples
--------
    # list the tools the server exposes
    python scripts/stdio_client.py list-tools

    # subscribe to a feed (uses the same launcher as a real MCP client)
    python scripts/stdio_client.py call add_feed url=https://blog.python.org

    # read back the newest 5 entries
    python scripts/stdio_client.py call list_entries limit=5

    # call a tool and print only its text block
    python scripts/stdio_client.py call server_info --text

The server is launched as ``python -m rss_mcp --db ./data/feeds.db`` and can be
re-pointed with ``--db``; arguments after the tool name are ``key=value`` pairs
where ``true``/``false``/integers/JSON arrays are parsed automatically.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

ROOT = Path(__file__).resolve().parent.parent


def _parse_value(raw: str) -> object:
    lowered = raw.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if lowered in {"null", "none"}:
        return None
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    if raw[:1] in "[{":
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass
    return raw


async def run(args: argparse.Namespace) -> int:
    env = {
        **os.environ,
        "RSS_MCP_DB": str(Path(args.db).expanduser().resolve()),
        "RSS_MCP_ALLOW_PRIVATE_HOSTS": "1" if args.allow_private else "0",
        "RSS_MCP_LOG_LEVEL": args.log_level,
    }
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "rss_mcp", "--db", env["RSS_MCP_DB"], "--log-level", args.log_level],
        env=env,
        cwd=str(ROOT),
    )
    async with (
        stdio_client(params) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as client,
    ):
        await client.initialize()

        if args.command == "list-tools":
            for tool in (await client.list_tools()).tools:
                first_line = (tool.description or "").splitlines()[0] if tool.description else ""
                print(f"{tool.name:22s} {first_line}")
            return 0

        if args.command == "list-resources":
            for resource in (await client.list_resources()).resources:
                print(f"{resource.uri}\t{resource.name or ''}")
            for template in (await client.list_resource_templates()).resource_templates:
                print(f"{template.uri_template}\t(template)")
            return 0

        if args.command == "read-resource":
            contents = await client.read_resource(args.target)
            for block in contents.contents:
                print(getattr(block, "text", block))
            return 0

        arguments: dict[str, object] = {}
        for pair in args.tool_arguments:
            if "=" not in pair:
                raise SystemExit(f"expected key=value, got {pair!r}")
            key, value = pair.split("=", 1)
            arguments[key] = _parse_value(value)

        result = await client.call_tool(args.tool, arguments)
        if getattr(result, "is_error", False):
            print("ERROR:", result.content[0].text if result.content else "", file=sys.stderr)
            return 1
        if args.text or result.structured_content is None:
            for block in result.content or []:
                print(getattr(block, "text", block))
        else:
            print(json.dumps(result.structured_content, indent=2, ensure_ascii=False))
        return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--db", default=str(ROOT / "data" / "feeds.db"), help="SQLite file")
    parser.add_argument("--log-level", default="WARNING")
    parser.add_argument(
        "--allow-private", action="store_true", help="allow localhost/LAN feed URLs"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list-tools", help="print the tool catalogue")
    sub.add_parser("list-resources", help="print resources and resource templates")

    read = sub.add_parser("read-resource", help="read one resource URI")
    read.add_argument("target", help="e.g. rss://feeds")

    call_parser = sub.add_parser("call", help="call one tool: call <tool> key=value ...")
    call_parser.add_argument("tool")
    call_parser.add_argument("tool_arguments", nargs="*", metavar="key=value")
    call_parser.add_argument("--text", action="store_true", help="print the text block only")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
