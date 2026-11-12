"""Small HTML/text helpers used when turning feed payloads into model-friendly text."""

from __future__ import annotations

import re
from html import unescape
from html.parser import HTMLParser

_WHITESPACE = re.compile(r"\s+")

# Tags that should be treated as word/paragraph boundaries when flattening HTML.
_BLOCK_TAGS = frozenset(
    {
        "p",
        "br",
        "div",
        "li",
        "ul",
        "ol",
        "tr",
        "td",
        "th",
        "table",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "blockquote",
        "pre",
        "section",
        "article",
        "header",
        "footer",
        "figure",
        "figcaption",
        "hr",
    }
)
_SKIP_TAGS = frozenset({"script", "style", "noscript", "template"})


class _TextExtractor(HTMLParser):
    """Flatten HTML to plain text, dropping script/style content."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag in _BLOCK_TAGS:
            self._parts.append(" ")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _BLOCK_TAGS:
            self._parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            if self._skip_depth:
                self._skip_depth -= 1
        elif tag in _BLOCK_TAGS:
            self._parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self._parts.append(data)

    def text(self) -> str:
        return _WHITESPACE.sub(" ", "".join(self._parts)).strip()


def html_to_text(value: str | None) -> str:
    """Return a whitespace-normalised plain-text rendering of an HTML fragment."""

    if not value:
        return ""
    if "<" not in value:
        return _WHITESPACE.sub(" ", unescape(value)).strip()
    parser = _TextExtractor()
    try:
        parser.feed(value)
        parser.close()
    except Exception:  # noqa: BLE001 - parser is best-effort; fall back to a regex
        return _WHITESPACE.sub(" ", unescape(re.sub(r"<[^>]+>", " ", value))).strip()
    return parser.text()


def truncate(value: str | None, limit: int | None) -> tuple[str | None, bool]:
    """Cut ``value`` to at most ``limit`` characters.

    Returns ``(text, was_truncated)``.
    """

    if value is None:
        return None, False
    if not limit or limit <= 0 or len(value) <= limit:
        return value, False
    return value[: max(0, limit - 3)].rstrip() + "...", True


def normalise_whitespace(value: str) -> str:
    return _WHITESPACE.sub(" ", value).strip()
