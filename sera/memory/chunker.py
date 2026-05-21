"""Heading-aware markdown chunker.

Splits a markdown / plaintext document into chunks of ≤ `max_tokens`,
respecting structural boundaries in this order:

  1. ATX headings (`# `, `## `, ...) reset the chunk and update the heading
     stack. Each chunk records its full `heading_path` tuple so retrieval
     hits read like Wikipedia citations ("Introduction > Background").
  2. Paragraphs (blank-line separated) are the primary split unit within a
     section.
  3. Lines split paragraphs that single-handedly exceed the budget.

Adjacent chunks within the same section share ~`overlap_ratio` of their
tokens so a chunk's tail is repeated as the next chunk's head — keeps
context contiguous for downstream retrieval without re-running the splitter.

Outclass: most rivals chunk on raw character count and lose the heading
hierarchy. Sera carries the full path on every chunk so search snippets
always say where they came from.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Sequence

from sera.context.tokens import estimate

DEFAULT_MAX_TOKENS = 3000
DEFAULT_OVERLAP_RATIO = 0.1

_ATX_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")


@dataclass(frozen=True)
class Chunk:
    """One contiguous span of a document.

    `heading_path` is the stack of ATX headings active at chunk start —
    empty tuple for pre-heading material. `start_line` / `end_line` are
    1-indexed line numbers in the original document for traceability.
    """

    content: str
    heading_path: tuple[str, ...] = ()
    start_line: int = 1
    end_line: int = 1
    token_count: int = 0

    @property
    def heading_chain(self) -> str:
        """Human-readable breadcrumb: 'Intro > Background > Goals'."""
        return " > ".join(self.heading_path)


def _classify_heading(line: str) -> tuple[int, str] | None:
    """Return (level, text) for an ATX heading line, else None."""
    m = _ATX_HEADING_RE.match(line)
    if not m:
        return None
    return len(m.group(1)), m.group(2).strip()


def _update_path(path: list[str], level: int, text: str) -> list[str]:
    """Push a heading into the path stack, popping deeper levels first."""
    # Level is 1-indexed; we keep `path` parallel to depth, so path[level-1]
    # becomes `text` and everything below is dropped.
    truncated = path[: level - 1]
    truncated.append(text)
    return truncated


def _split_paragraphs(lines: Sequence[str], start_line: int) -> list[tuple[str, int, int]]:
    """Group consecutive non-blank lines into paragraphs.

    Returns [(paragraph_text, start_line, end_line), ...] using the
    1-indexed line number in the *original document* (start_line + offset).
    """
    out: list[tuple[str, int, int]] = []
    buf: list[str] = []
    buf_start = start_line
    for i, ln in enumerate(lines):
        line_no = start_line + i
        if ln.strip() == "":
            if buf:
                out.append(("\n".join(buf), buf_start, line_no - 1))
                buf = []
            continue
        if not buf:
            buf_start = line_no
        buf.append(ln)
    if buf:
        out.append(("\n".join(buf), buf_start, start_line + len(lines) - 1))
    return out


def _split_paragraph_by_lines(
    paragraph: str, max_tokens: int, start_line: int
) -> list[tuple[str, int, int]]:
    """Last-resort: split a paragraph that exceeds `max_tokens` on its own."""
    lines = paragraph.split("\n")
    out: list[tuple[str, int, int]] = []
    buf: list[str] = []
    buf_tokens = 0
    buf_start = start_line
    for i, ln in enumerate(lines):
        line_no = start_line + i
        cost = estimate(ln) + 1  # +1 for the newline boundary
        if buf and buf_tokens + cost > max_tokens:
            out.append(("\n".join(buf), buf_start, line_no - 1))
            buf = []
            buf_tokens = 0
            buf_start = line_no
        buf.append(ln)
        buf_tokens += cost
    if buf:
        out.append(("\n".join(buf), buf_start, start_line + len(lines) - 1))
    return out


def _carry_overlap(text: str, target_tokens: int) -> str:
    """Take the trailing slice of `text` worth ~`target_tokens` tokens.

    Splits on lines so we never end mid-word. If the entire chunk is
    smaller than the target, return the whole thing — the caller decides
    whether to deduplicate.
    """
    if target_tokens <= 0 or not text:
        return ""
    lines = text.split("\n")
    if not lines:
        return ""
    carried: list[str] = []
    carried_tokens = 0
    for ln in reversed(lines):
        cost = estimate(ln) + 1
        if carried_tokens + cost > target_tokens and carried:
            break
        carried.append(ln)
        carried_tokens += cost
    return "\n".join(reversed(carried))


@dataclass
class _Builder:
    """Mutable state that accumulates lines into a single chunk."""

    heading_path: tuple[str, ...] = ()
    parts: list[str] = field(default_factory=list)
    tokens: int = 0
    start_line: int = 1
    end_line: int = 1

    def reset(self, *, heading_path: tuple[str, ...], start_line: int) -> None:
        self.heading_path = heading_path
        self.parts = []
        self.tokens = 0
        self.start_line = start_line
        self.end_line = start_line

    def add(self, text: str, *, end_line: int, token_cost: int) -> None:
        self.parts.append(text)
        self.tokens += token_cost
        self.end_line = end_line

    def is_empty(self) -> bool:
        return not self.parts

    def build(self) -> Chunk:
        content = "\n\n".join(self.parts)
        return Chunk(
            content=content,
            heading_path=self.heading_path,
            start_line=self.start_line,
            end_line=self.end_line,
            token_count=self.tokens,
        )


def chunk_markdown(
    text: str,
    *,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    overlap_ratio: float = DEFAULT_OVERLAP_RATIO,
) -> list[Chunk]:
    """Split markdown into heading-aware chunks ≤ `max_tokens`.

    The current heading stack is captured at the *start* of each chunk —
    if a heading lands inside a chunk's span, we'd cut and start a new
    one, so the heading_path of the next chunk reflects the new section.
    """
    if not text.strip():
        return []
    if not 0.0 <= overlap_ratio < 1.0:
        raise ValueError(f"overlap_ratio out of range: {overlap_ratio}")
    if max_tokens < 1:
        raise ValueError(f"max_tokens must be ≥ 1, got {max_tokens}")

    overlap_target = int(max_tokens * overlap_ratio)

    lines = text.splitlines()
    chunks: list[Chunk] = []
    heading_stack: list[str] = []
    builder = _Builder(heading_path=(), start_line=1)

    def flush_with_overlap() -> None:
        """Emit the current builder and seed a fresh one with the overlap tail."""
        if builder.is_empty():
            return
        chunks.append(builder.build())
        tail = _carry_overlap(chunks[-1].content, overlap_target)
        next_start_line = builder.end_line  # tail repeats prior lines
        builder.reset(
            heading_path=tuple(heading_stack),
            start_line=next_start_line,
        )
        if tail:
            builder.add(tail, end_line=next_start_line, token_cost=estimate(tail))

    i = 0
    while i < len(lines):
        line = lines[i]
        line_no = i + 1
        heading = _classify_heading(line)
        if heading is not None:
            level, text_head = heading
            # New heading boundary → finish whatever's in the builder, no
            # overlap (heading sections are deliberately independent).
            if not builder.is_empty():
                chunks.append(builder.build())
            heading_stack[:] = _update_path(heading_stack, level, text_head)
            # The heading line itself becomes the first content of the next
            # chunk; the heading_path reflects the new stack.
            head_line = ("#" * level) + " " + text_head
            builder.reset(
                heading_path=tuple(heading_stack), start_line=line_no
            )
            builder.add(head_line, end_line=line_no, token_cost=estimate(head_line))
            i += 1
            continue

        # Gather a paragraph (consecutive non-blank lines).
        if line.strip() == "":
            i += 1
            continue
        para_lines: list[str] = []
        para_start = line_no
        j = i
        while j < len(lines) and lines[j].strip() != "":
            if _classify_heading(lines[j]) is not None:
                break  # don't swallow a heading into a paragraph
            para_lines.append(lines[j])
            j += 1
        para = "\n".join(para_lines)
        para_end = para_start + len(para_lines) - 1
        para_tokens = estimate(para)

        if para_tokens > max_tokens:
            # Oversize paragraph → split by line. Flush whatever's pending
            # so the split paragraph stays grouped.
            flush_with_overlap()
            pieces = _split_paragraph_by_lines(para, max_tokens, para_start)
            for piece_text, ps, pe in pieces:
                if not builder.is_empty():
                    chunks.append(builder.build())
                    builder.reset(
                        heading_path=tuple(heading_stack), start_line=ps
                    )
                piece_tokens = estimate(piece_text)
                builder.add(piece_text, end_line=pe, token_cost=piece_tokens)
                if builder.tokens >= max_tokens:
                    flush_with_overlap()
            i = j
            continue

        if builder.tokens + para_tokens > max_tokens and not builder.is_empty():
            flush_with_overlap()
        builder.add(para, end_line=para_end, token_cost=para_tokens)
        i = j

    if not builder.is_empty():
        chunks.append(builder.build())

    return chunks


def chunk_text(
    text: str,
    *,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    overlap_ratio: float = DEFAULT_OVERLAP_RATIO,
) -> list[Chunk]:
    """Chunk plain text without expecting markdown structure.

    Equivalent to `chunk_markdown` on input with no headings — paragraphs
    are still respected, line-split is still the fallback for oversize
    paragraphs, but `heading_path` will be empty on every chunk.
    """
    return chunk_markdown(text, max_tokens=max_tokens, overlap_ratio=overlap_ratio)
