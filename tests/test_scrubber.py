"""StreamingContextScrubber boundary cases."""
from __future__ import annotations

from sera.context.scrubber import StreamingContextScrubber, scrub


def test_plain_text_passthrough():
    assert scrub("hello world") == "hello world"


def test_strips_complete_span():
    out = scrub("before <context>private</context> after")
    assert out == "before  after"


def test_strips_memory_context_variant():
    out = scrub("a<memory-context>b</memory-context>c")
    assert out == "ac"


def test_unrelated_angle_brackets_preserved():
    assert scrub("if a<b and b>c then") == "if a<b and b>c then"


def test_split_open_tag_across_chunks():
    s = StreamingContextScrubber()
    parts = ["before <con", "text>secret</con", "text> after"]
    out = "".join(s.feed(p) for p in parts) + s.flush()
    assert out == "before  after"


def test_split_close_tag_across_chunks():
    s = StreamingContextScrubber()
    parts = ["a<context>b", "c</cont", "ext>d"]
    out = "".join(s.feed(p) for p in parts) + s.flush()
    assert out == "ad"


def test_byte_by_byte_feed():
    text = "x<context>y</context>z"
    s = StreamingContextScrubber()
    out = "".join(s.feed(ch) for ch in text) + s.flush()
    assert out == "xz"


def test_unclosed_span_at_eof_is_dropped():
    s = StreamingContextScrubber()
    out = s.feed("safe <context>danger") + s.flush()
    assert out == "safe "


def test_multiple_spans():
    out = scrub("a<context>1</context>b<context>2</context>c")
    assert out == "abc"


def test_only_text_no_brackets():
    # Long text with no `<` triggers the no-`<` fast path.
    big = "a" * 10000
    assert scrub(big) == big
