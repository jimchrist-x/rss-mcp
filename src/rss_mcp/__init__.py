"""rss-mcp -- an MCP server keeping RSS/Atom feeds in SQLite.

Import :mod:`rss_mcp.server` for the MCP server object (``mcp``) and the CLI
entry point (``main``).
"""

from .config import VERSION

__version__ = VERSION
__all__ = ["__version__"]
