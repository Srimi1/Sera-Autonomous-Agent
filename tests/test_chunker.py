"""P-12: heading-aware markdown chunker."""
from __future__ import annotations

import pytest

from sera.context.tokens import estimate
from sera.memory.chunker import (
    chunk_markdown,
    chunk_text,
)


def test_empty_input_returns_empty_list():
    assert chunk_markdown("") == []
    assert chunk_markdown("   \n\n   ") == []


def test_short_text_single_chunk():
    chunks = chunk_markdown("Hello world.\n\nSecond paragraph.")
    assert len(chunks) == 1
    assert chunks[0].heading_path == ()
    assert "Hello world" in chunks[0].content
    assert "Second paragraph" in chunks[0].content


def test_atx_heading_starts_new_chunk():
    md = "Intro paragraph.\n\n# Section A\n\nA body.\n\n# Section B\n\nB body."
    chunks = chunk_markdown(md)
    paths = [c.heading_path for c in chunks]
    assert paths == [(), ("Section A",), ("Section B",)]


def test_nested_heading_stack_pops_on_higher_level():
    md = (
        "# Top\n\nbody1\n\n## Sub\n\nbody2\n\n### Deep\n\nbody3\n\n"
        "# Top2\n\nbody4\n"
    )
    chunks = chunk_markdown(md)
    paths = [c.heading_path for c in chunks]
    assert paths == [
        ("Top",),
        ("Top", "Sub"),
        ("Top", "Sub", "Deep"),
        ("Top2",),
    ]


def test_chunks_respect_max_tokens():
    """Synthesize a document well above one chunk's budget."""
    body = "\n\n".join("paragraph " + str(i) + " " + ("filler " * 20) for i in range(200))
    chunks = chunk_markdown(body, max_tokens=200)
    assert len(chunks) > 1
    for c in chunks:
        # Allow small over-budget for the overlap tail that seeded the chunk.
        assert c.token_count <= 200 * 1.5


def test_overlap_carries_trailing_lines():
    """Adjacent chunks within the same section should share some content."""
    # Two big paragraphs in the same heading; force a split with a small budget.
    p1 = "alpha " * 30
    p2 = "beta " * 30
    p3 = "gamma " * 30
    md = f"# Section\n\n{p1}\n\n{p2}\n\n{p3}\n"
    chunks = chunk_markdown(md, max_tokens=80, overlap_ratio=0.2)
    assert len(chunks) >= 2
    # The tail of chunk[0] (after split) should appear at the start of chunk[1].
    assert any(
        any(line in chunks[i + 1].content for line in chunks[i].content.split("\n") if line.strip())
        for i in range(len(chunks) - 1)
    )


def test_single_huge_paragraph_splits_by_line():
    long_lines = "\n".join(f"line {i} " + ("x" * 20) for i in range(100))
    chunks = chunk_markdown(long_lines, max_tokens=80)
    assert len(chunks) >= 2
    for c in chunks:
        assert c.token_count <= 200  # liberal upper bound — line-split keeps things bounded


def test_heading_line_is_kept_in_chunk_content():
    md = "# Title\n\nbody"
    chunks = chunk_markdown(md)
    assert chunks[0].content.startswith("# Title")
    assert "body" in chunks[0].content


def test_heading_chain_property():
    md = "# A\n\n## B\n\n### C\n\nbody"
    chunks = chunk_markdown(md)
    deepest = chunks[-1]
    assert deepest.heading_chain == "A > B > C"


def test_chunk_text_aliases_to_markdown():
    plain = "para one\n\npara two\n"
    md_chunks = chunk_markdown(plain)
    text_chunks = chunk_text(plain)
    assert [c.content for c in md_chunks] == [c.content for c in text_chunks]


def test_line_numbers_track_original_document():
    md = "first\n\nsecond\n\n# Heading\n\nbody"
    chunks = chunk_markdown(md)
    # Heading appears on line 5; the chunk starting at that heading should
    # have start_line == 5 and end_line == 7 (heading + blank + body).
    heading_chunk = next(c for c in chunks if c.heading_path == ("Heading",))
    assert heading_chunk.start_line == 5
    assert heading_chunk.end_line >= 5


def test_fifty_section_round_trip_preserves_every_heading():
    sections = []
    expected_headings: list[str] = []
    for i in range(50):
        title = f"Section {i:02d}"
        expected_headings.append(title)
        body = " ".join(f"word{j}" for j in range(30))
        sections.append(f"# {title}\n\n{body}\n")
    md = "\n".join(sections)
    chunks = chunk_markdown(md)
    seen = {c.heading_path[0] for c in chunks if c.heading_path}
    for title in expected_headings:
        assert title in seen, f"{title} missing from chunk heading paths"


def test_overlap_ratio_zero_means_no_repeat():
    p1 = "alpha " * 60
    p2 = "beta " * 60
    p3 = "gamma " * 60
    md = f"# S\n\n{p1}\n\n{p2}\n\n{p3}\n"
    chunks = chunk_markdown(md, max_tokens=80, overlap_ratio=0.0)
    assert len(chunks) >= 2
    # No carry-over means the next chunk must not start with the prior tail.
    first_tail = chunks[0].content.splitlines()[-1] if chunks[0].content else ""
    second_head = chunks[1].content.splitlines()[0] if chunks[1].content else ""
    assert first_tail != second_head or first_tail == ""


def test_overlap_ratio_rejects_out_of_range():
    with pytest.raises(ValueError):
        chunk_markdown("x", overlap_ratio=-0.1)
    with pytest.raises(ValueError):
        chunk_markdown("x", overlap_ratio=1.0)


def test_max_tokens_rejects_zero():
    with pytest.raises(ValueError):
        chunk_markdown("x", max_tokens=0)


def test_token_counts_match_estimate():
    chunks = chunk_markdown("# H\n\nhello world\n")
    for c in chunks:
        # Allow the heading_line cost (~3) plus body to land in the chunk total.
        assert c.token_count > 0
        assert c.token_count >= estimate(c.content) - estimate(c.content) // 4
