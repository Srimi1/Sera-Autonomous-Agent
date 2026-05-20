"""P-08: TokenJuice rule pipeline + LLM-fallback orchestrator."""
from __future__ import annotations

import asyncio

import pytest

from sera.context.tokenjuice import (
    DEFAULT_TABLE_ROW_LIMIT,
    compress,
    compress_sync,
    debloat_table,
    dedup_lines,
    html_to_markdown,
    looks_like_html,
    normalize_whitespace,
    shorten_urls,
    strip_ansi,
)


def test_strip_ansi_removes_color_codes():
    s = "\x1b[31mERROR\x1b[0m: \x1b[1mboom\x1b[0m"
    assert strip_ansi(s) == "ERROR: boom"


def test_looks_like_html_signals_html_only():
    assert looks_like_html("<html><body>hi</body></html>")
    assert looks_like_html("hello <p>world</p>")
    assert not looks_like_html("plain text, no tags here")


def test_html_to_markdown_basic_shapes():
    html = "<h1>Title</h1><p>Para <strong>bold</strong> end.</p><ul><li>a</li><li>b</li></ul>"
    md = html_to_markdown(html)
    assert "# Title" in md
    assert "**bold**" in md
    assert "- a" in md and "- b" in md


def test_html_to_markdown_drops_scripts_and_styles():
    html = (
        "<html><head><style>body{color:red}</style></head>"
        "<body><script>alert('xss')</script><p>real text</p></body></html>"
    )
    md = html_to_markdown(html)
    assert "alert" not in md
    assert "color:red" not in md
    assert "real text" in md


def test_html_to_markdown_passthrough_for_plain_text():
    text = "no tags, just words"
    assert html_to_markdown(text) == text


def test_html_links_preserve_href():
    md = html_to_markdown('<a href="https://example.com">click</a>')
    assert "[click](https://example.com)" in md


def test_shorten_urls_keeps_short_urls():
    text = "see https://a.io/x for more"
    assert shorten_urls(text) == text


def test_shorten_urls_replaces_long_urls_with_host_annotation():
    long = "https://example.com/" + ("path/" * 30) + "?q=" + ("x" * 50)
    text = f"link: {long} done"
    out = shorten_urls(text)
    assert long not in out
    assert "example.com" in out
    assert f"({len(long)}c)" in out


def test_dedup_lines_collapses_adjacent_runs():
    text = "boot\nwarn\nwarn\nwarn\nready\nready"
    out = dedup_lines(text)
    assert out == "boot\nwarn  … (x3)\nready  … (x2)"


def test_dedup_lines_preserves_non_adjacent_repeats():
    text = "a\nb\na\nb"
    assert dedup_lines(text) == text


def test_normalize_whitespace():
    text = "line1   \n\n\n\nline2  \n   \nline3\n\n\n"
    out = normalize_whitespace(text)
    assert out == "line1\n\nline2\n\nline3"


def test_debloat_table_drops_empty_columns():
    table = (
        "| name | age | nick | notes |\n"
        "| --- | --- | --- | --- |\n"
        "| Sera | 1 |  | - |\n"
        "| Hermes | 2 |  | n/a |\n"
    )
    out = debloat_table(table)
    assert "nick" not in out
    assert "notes" not in out
    assert "name" in out and "age" in out


def test_debloat_table_clips_long_tables():
    rows = [f"| r{i} | v{i} |" for i in range(DEFAULT_TABLE_ROW_LIMIT + 25)]
    table = "| col | val |\n| --- | --- |\n" + "\n".join(rows)
    out = debloat_table(table)
    assert f"({25} more rows)" in out
    assert "r0" in out
    assert "r74" not in out  # past the clip


def test_compress_sync_shrinks_html_bench():
    html = (
        "<html><head><style>"
        + ("body{color:red}" * 100)
        + "</style><script>"
        + ("alert('x');" * 100)
        + "</script></head><body>"
        + "<nav>" + ("<a href='/x'>x</a>" * 30) + "</nav>"
        + "<p>The real content is short.</p>"
        + "</body></html>"
    )
    result = compress_sync(html)
    # ≥30% shrink target per phase verification.
    assert result.shrink_ratio >= 0.30
    assert "html" in result.rules_applied
    assert "alert" not in result.text


def test_compress_sync_shrinks_repeated_log_bench():
    line = (
        "2026-05-20T12:00:00 WARN connection reset, retrying    \x1b[0m"
    )
    text = "\n".join([line] * 200)
    result = compress_sync(text)
    assert result.shrink_ratio >= 0.30
    assert "ansi" in result.rules_applied
    assert "dedup" in result.rules_applied


def test_compress_sync_handles_empty():
    result = compress_sync("")
    assert result.text == ""
    assert result.original_chars == 0


def test_compress_sync_redacts_secrets():
    text = "x" * 600 + "\napi key: sk-ant-api01-" + "A" * 30
    result = compress_sync(text)
    assert "sk-ant-api01" not in result.text
    assert "<redacted:anthropic-key>" in result.text


def test_compress_async_skips_fallback_when_under_cap():
    async def boom(_t: str) -> str:
        raise AssertionError("LLM fallback should not be called")

    result = asyncio.run(compress("hello world", max_tokens=1000, llm_fallback=boom))
    assert not result.llm_fallback_used


def test_compress_async_invokes_fallback_when_over_cap():
    big = ("the quick brown fox " * 500)

    async def shrink(text: str) -> str:
        return "[fallback summary]"

    result = asyncio.run(compress(big, max_tokens=50, llm_fallback=shrink))
    assert result.llm_fallback_used
    assert result.text == "[fallback summary]"
    assert "llm-fallback" in result.rules_applied


def test_compress_async_without_fallback_returns_rule_output_even_if_oversize():
    big = "x" * 5000
    result = asyncio.run(compress(big, max_tokens=10, llm_fallback=None))
    assert not result.llm_fallback_used
