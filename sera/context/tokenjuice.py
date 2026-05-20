"""TokenJuice — rule-based tool-output compressor with optional LLM fallback.

Tool results dominate token spend on web-scraping + shell-heavy turns. This
module runs a deterministic pipeline of cheap rewrites before the result
reaches the LLM:

  1. Strip ANSI CSI escapes (shell colour codes).
  2. Collapse HTML to lightweight Markdown (no JS, no styles, no nav cruft).
  3. Shorten long URLs to `<host…/path (Nchar)>` annotations.
  4. Dedup adjacent identical lines (`… (xN)` marker).
  5. Trim a markdown table's empty columns + clip to N rows.
  6. Normalize whitespace (trailing spaces, 3+ blank lines → 2).
  7. Redact secrets (reuse safety.redact patterns; defense in depth).

The orchestrator `compress(text, max_tokens=None, llm_fallback=None)` runs
every rule, then — if the output is still over `max_tokens` and a callable
fallback is provided — hands the leftover to a cheap-model summarizer.

Outclass: OpenHuman / OpenClaw ship rules-only output trimming. Sera adds
the LLM-fallback escape hatch for hard cases (dense logs, structured
diagnostics) where rules alone can't compress without losing meaning.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Awaitable, Callable

from sera.context.tokens import estimate
from sera.safety.redact import redact

DEFAULT_URL_THRESHOLD = 60
"""URLs longer than this get shortened to a host+ellipsis annotation."""

DEFAULT_TABLE_ROW_LIMIT = 50
"""Markdown tables longer than this get clipped with a `… (N more)` marker."""

DEFAULT_COMPRESS_THRESHOLD = 500
"""Outputs shorter than this skip the rule pipeline (it would be churn)."""

LLMFallback = Callable[[str], Awaitable[str]]
"""Async callable invoked when rules can't get the output under the cap.

Receives the rule-compressed text, returns a further-shrunk version. Caller
is responsible for picking a cheap model + capping its max_tokens.
"""


@dataclass(frozen=True)
class CompressionResult:
    """Output of `compress()`. Includes the before/after sizes for telemetry."""

    text: str
    original_chars: int
    final_chars: int
    rules_applied: tuple[str, ...]
    llm_fallback_used: bool

    @property
    def shrink_ratio(self) -> float:
        if not self.original_chars:
            return 0.0
        return 1.0 - (self.final_chars / self.original_chars)


# ─── ANSI ──────────────────────────────────────────────────────────────────

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


# ─── HTML → Markdown ───────────────────────────────────────────────────────


class _HtmlToMarkdown(HTMLParser):
    """Minimal HTML → Markdown converter. Drops script/style/nav. No JS exec."""

    _SKIP_TAGS = {"script", "style", "noscript", "svg", "head"}
    _BLOCK_TAGS = {"p", "div", "section", "article", "header", "footer", "main"}
    _HEADINGS = {"h1": "#", "h2": "##", "h3": "###", "h4": "####", "h5": "#####", "h6": "######"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._out: list[str] = []
        self._skip_depth = 0
        self._in_pre = 0
        self._href_stack: list[str] = []
        self._list_stack: list[str] = []  # "ul" or "ol"

    def _emit(self, s: str) -> None:
        if not s:
            return
        self._out.append(s)

    def handle_starttag(self, tag, attrs):  # noqa: ANN001
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        a = dict(attrs)
        if tag == "br":
            self._emit("\n")
        elif tag == "hr":
            self._emit("\n\n---\n\n")
        elif tag in self._HEADINGS:
            self._emit(f"\n\n{self._HEADINGS[tag]} ")
        elif tag in self._BLOCK_TAGS:
            self._emit("\n\n")
        elif tag in {"strong", "b"}:
            self._emit("**")
        elif tag in {"em", "i"}:
            self._emit("*")
        elif tag == "code" and not self._in_pre:
            self._emit("`")
        elif tag == "pre":
            self._in_pre += 1
            self._emit("\n```\n")
        elif tag in {"ul", "ol"}:
            self._list_stack.append(tag)
            self._emit("\n")
        elif tag == "li":
            marker = "- " if (self._list_stack and self._list_stack[-1] == "ul") else "1. "
            self._emit(f"\n{marker}")
        elif tag == "a":
            self._href_stack.append(a.get("href", ""))
            self._emit("[")

    def handle_endtag(self, tag):  # noqa: ANN001
        if tag in self._SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if tag in self._HEADINGS or tag in self._BLOCK_TAGS:
            self._emit("\n")
        elif tag in {"strong", "b"}:
            self._emit("**")
        elif tag in {"em", "i"}:
            self._emit("*")
        elif tag == "code" and not self._in_pre:
            self._emit("`")
        elif tag == "pre":
            self._in_pre = max(0, self._in_pre - 1)
            self._emit("\n```\n")
        elif tag in {"ul", "ol"} and self._list_stack:
            self._list_stack.pop()
            self._emit("\n")
        elif tag == "a" and self._href_stack:
            href = self._href_stack.pop()
            self._emit(f"]({href})" if href else "]")

    def handle_data(self, data):  # noqa: ANN001
        if self._skip_depth:
            return
        self._out.append(data)

    def result(self) -> str:
        return "".join(self._out)


_HTML_SNIFF = re.compile(r"<(?:html|body|div|p|a|h[1-6]|table|ul|ol|article)\b", re.IGNORECASE)


def looks_like_html(text: str) -> bool:
    return bool(_HTML_SNIFF.search(text[:4096]))


def html_to_markdown(text: str) -> str:
    if not looks_like_html(text):
        return text
    parser = _HtmlToMarkdown()
    try:
        parser.feed(text)
        parser.close()
    except Exception:  # noqa: BLE001 — malformed HTML, give up cleanly
        return text
    return parser.result()


# ─── URL shortening ────────────────────────────────────────────────────────

_URL_RE = re.compile(r"https?://[^\s<>\"')]+", re.IGNORECASE)
_HOST_RE = re.compile(r"^https?://([^/]+)")


def shorten_urls(text: str, threshold: int = DEFAULT_URL_THRESHOLD) -> str:
    def repl(m: re.Match[str]) -> str:
        url = m.group(0)
        if len(url) <= threshold:
            return url
        host_m = _HOST_RE.match(url)
        host = host_m.group(1) if host_m else "?"
        return f"<{host}/… ({len(url)}c)>"

    return _URL_RE.sub(repl, text)


# ─── Line dedup ────────────────────────────────────────────────────────────


def dedup_lines(text: str) -> str:
    """Collapse runs of identical adjacent lines.

    Common in tail/grep/uvicorn output where the same warning repeats. We
    keep the first occurrence and append `… (xN)` when N > 1.
    """
    if "\n" not in text:
        return text
    out: list[str] = []
    prev: str | None = None
    count = 0
    for line in text.split("\n"):
        if line == prev:
            count += 1
            continue
        if prev is not None and count > 1:
            out.append(f"{prev}  … (x{count})")
        elif prev is not None:
            out.append(prev)
        prev = line
        count = 1
    if prev is not None:
        if count > 1:
            out.append(f"{prev}  … (x{count})")
        else:
            out.append(prev)
    return "\n".join(out)


# ─── Whitespace normalize ──────────────────────────────────────────────────

_BLANK_RUN = re.compile(r"\n{3,}")
_TRAILING_WS = re.compile(r"[ \t]+\n")


def normalize_whitespace(text: str) -> str:
    text = _TRAILING_WS.sub("\n", text)
    text = _BLANK_RUN.sub("\n\n", text)
    return text.strip()


# ─── Table debloat ─────────────────────────────────────────────────────────

_TABLE_LINE_RE = re.compile(r"^\s*\|.+\|\s*$")
_EMPTY_CELL_VALUES = {"", "-", "—", "n/a", "na", "null", "none"}


def _is_table_block(lines: list[str], start: int) -> int:
    """Return the count of consecutive table-shaped lines starting at `start`, or 0."""
    n = 0
    while start + n < len(lines) and _TABLE_LINE_RE.match(lines[start + n]):
        n += 1
    return n if n >= 2 else 0  # at least header + one body row


def _split_row(row: str) -> list[str]:
    inner = row.strip()
    if inner.startswith("|"):
        inner = inner[1:]
    if inner.endswith("|"):
        inner = inner[:-1]
    return [c.strip() for c in inner.split("|")]


def _join_row(cells: list[str]) -> str:
    return "| " + " | ".join(cells) + " |"


def debloat_table(text: str, row_limit: int = DEFAULT_TABLE_ROW_LIMIT) -> str:
    """Drop empty columns; clip body rows past `row_limit` with a summary."""
    if "|" not in text:
        return text
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        block_len = _is_table_block(lines, i)
        if block_len == 0:
            out.append(lines[i])
            i += 1
            continue

        rows = [_split_row(lines[i + j]) for j in range(block_len)]
        width = max(len(r) for r in rows)
        rows = [r + [""] * (width - len(r)) for r in rows]

        # Identify empty columns by scanning every body row (skip separator row
        # which is typically `---`-only). Header is preserved.
        keep_cols = []
        for col in range(width):
            values = [rows[r][col].strip().lower() for r in range(len(rows))]
            non_separator = [v for v in values[1:] if not re.match(r"^[-:]+$", v)]
            if any(v not in _EMPTY_CELL_VALUES for v in non_separator):
                keep_cols.append(col)
            elif col == 0:
                # Always keep first column even if empty — preserves table shape.
                keep_cols.append(col)

        rows = [[r[c] for c in keep_cols] for r in rows]

        body_rows = rows[2:] if len(rows) > 2 and all(
            re.match(r"^[-:]+$", c.strip()) for c in rows[1] if c.strip()
        ) else rows[1:]
        header_rows = rows[: len(rows) - len(body_rows)]

        if len(body_rows) > row_limit:
            kept = body_rows[:row_limit]
            extra = len(body_rows) - row_limit
            kept.append([f"… ({extra} more rows)"] + [""] * (len(keep_cols) - 1))
            body_rows = kept

        for r in header_rows + body_rows:
            out.append(_join_row(r))
        i += block_len
    return "\n".join(out)


# ─── Orchestrator ──────────────────────────────────────────────────────────


def compress_sync(text: str, *, url_threshold: int = DEFAULT_URL_THRESHOLD) -> CompressionResult:
    """Run the rule pipeline without the optional LLM fallback. Pure + sync.

    Useful when you have no event loop or no fallback configured — the
    common path for the agent loop.
    """
    if not text:
        return CompressionResult(text="", original_chars=0, final_chars=0,
                                 rules_applied=(), llm_fallback_used=False)

    original = text
    applied: list[str] = []

    out = strip_ansi(text)
    if out != text:
        applied.append("ansi")
    text = out

    out = html_to_markdown(text)
    if out != text:
        applied.append("html")
    text = out

    out = shorten_urls(text, threshold=url_threshold)
    if out != text:
        applied.append("urls")
    text = out

    out = debloat_table(text)
    if out != text:
        applied.append("table")
    text = out

    out = dedup_lines(text)
    if out != text:
        applied.append("dedup")
    text = out

    out = normalize_whitespace(text)
    if out != text:
        applied.append("ws")
    text = out

    out = redact(text)
    if out != text:
        applied.append("redact")
    text = out

    return CompressionResult(
        text=text,
        original_chars=len(original),
        final_chars=len(text),
        rules_applied=tuple(applied),
        llm_fallback_used=False,
    )


async def compress(
    text: str,
    *,
    max_tokens: int | None = None,
    llm_fallback: LLMFallback | None = None,
    url_threshold: int = DEFAULT_URL_THRESHOLD,
) -> CompressionResult:
    """Async orchestrator: rules then optional LLM-fallback when oversize.

    `max_tokens` is the cap that triggers the fallback. Without it, the
    function is pure rules. The fallback is invoked exactly once and its
    return value is taken as-is (no recursive compression).
    """
    base = compress_sync(text, url_threshold=url_threshold)
    if max_tokens is None or llm_fallback is None:
        return base
    if estimate(base.text) <= max_tokens:
        return base
    summary = await llm_fallback(base.text)
    return CompressionResult(
        text=summary,
        original_chars=base.original_chars,
        final_chars=len(summary),
        rules_applied=base.rules_applied + ("llm-fallback",),
        llm_fallback_used=True,
    )
