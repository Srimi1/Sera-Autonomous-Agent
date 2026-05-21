# P-12 — Semantic chunker

## Status

done (shipped 2026-05-21, this session).

## Outclass claim

**Heading-aware metadata** — each chunk keeps its heading chain so search results read like Wikipedia citations.

## Goal

Split markdown / text into ≤ 3k-token chunks that respect document structure.

## Deliverables

- `sera/memory/chunker.py`:
  - `Chunk` dataclass — `content`, `heading_path` tuple, `start_line` / `end_line` (1-indexed), `token_count`, `heading_chain` breadcrumb property.
  - `chunk_markdown(text, max_tokens=3000, overlap_ratio=0.1)` — splits on ATX headings → paragraphs → lines, in that order. Maintains a depth-keyed heading stack: `# A` is depth 1, `## B` is depth 2; visiting a depth-N heading truncates the stack to N-1 entries and pushes the new heading.
  - `chunk_text` — alias for non-markdown content; same algorithm, headings just don't appear.
  - Overlap: `_carry_overlap` takes the trailing slice of the previous chunk worth ~`overlap_ratio * max_tokens` tokens, split on line boundaries so words don't break. Heading-boundary chunks skip the overlap deliberately — sections are independent units.
  - Oversized-paragraph fallback: `_split_paragraph_by_lines` hard-splits a single paragraph that exceeds `max_tokens` on its own.
- Validation: `overlap_ratio` ∈ [0, 1); `max_tokens ≥ 1`.

## Files touched

new `sera/memory/chunker.py`; new `tests/test_chunker.py` (16 tests).

## Verification

```bash
pytest -q tests/test_chunker.py        # 16 passed
pytest -q                               # 153 passed total (was 137 + 16 new)
python -m pyflakes sera/                # 0 warnings
```

## Dependencies

P-11.

## Notes

_Journal: decisions, blockers, commit refs go here._

**Design decisions (2026-05-21):**

- **No markdown library dep.** A regex-based ATX detector (`^#{1,6} .+`) plus blank-line paragraph grouping covers the formats Sera will scrape (READMEs, Obsidian notes, docs sites). Pulling in `markdown-it-py` or `mistune` just to find headings is overkill for skeleton retrieval.
- **Heading boundary = hard break.** A new heading flushes the current builder with NO overlap. Two sections should be independent retrieval targets; bleeding the end of one into the start of the other would smear the heading path metadata.
- **Within-section overlap is line-anchored.** `_carry_overlap` reverses through the prior chunk's lines and keeps appending until it crosses the budget — so the tail is full lines, not half-sentences. Token estimate keeps the carried weight bounded.
- **Setext headings (`====` underline) are NOT recognised.** ATX is the dominant form in machine-generated markdown (Obsidian, GitHub, Notion exports). Setext support is a follow-up if real source corpora demand it.
- **`token_count` is best-effort.** `_Builder.tokens` sums per-paragraph `estimate()` calls plus the heading line; this can drift slightly from `estimate(content)` because join-spacing changes token boundaries. Tests assert it stays within ~25% — close enough for the ranker that consumes it.
- **Skeleton does not embed.** Producing `embedding` is P-13's job. `chunk_markdown` returns Chunks with `embedding=None` implied — they slot directly into `MemoryTree.add_chunk(embedding=…)` once an embedder is wired.
- **No mid-chunk heading injection.** When a heading lands mid-paragraph (legal in CommonMark only as a line break), the inner loop stops the paragraph at that point so heading detection still wins. Keeps `heading_path` accurate.
- **Line numbers are absolute in the original document** — useful when a chunk's source is a file the agent later edits. Adjacent chunks within the same overlap region can share lines, by design.
