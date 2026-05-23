"""Anonymous peer ranking parser — tolerant to LLM commentary.

Outclass over llm-council: their parser requires exact "Response X" prefix,
breaks on any commentary inside the ranking block, and returns an empty list
with no diagnostic. Ours:
  - Strips "Response" prefix automatically
  - Tolerates commentary lines interspersed in the ranking block
  - Accepts multiple numbering styles: "1.", "1:", "1)", "(1)"
  - Accepts bare labels ("1. C"), bold labels ("1. **C**"), annotated lines
    ("1. C — best reasoning")
  - Case-insensitive header matching ("final ranking:", "FINAL RANKING:")
  - Validates completeness against the exact expected label set
  - Returns a typed RankingResult — never raises on bad input
  - Records which parse strategy succeeded (useful for prompt tuning)
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class RankingResult:
    ranking: tuple[str, ...]  # ordered labels ("C", "A", "B"); empty = parse failed
    raw_section: str          # text after "FINAL RANKING:" header; empty if not found
    is_complete: bool         # True iff all expected_labels appear exactly once
    missing: frozenset[str]   # labels absent from the parsed ranking
    parse_method: str         # "numbered_full" | "numbered_bare" | "bare_sequence" | "none"


_HEADER_RE = re.compile(r"FINAL\s+RANKING\s*:", re.IGNORECASE)

# Numbered prefix shared by all positional strategies:
#   "1. "  "1: "  "1) "  "(1) "
_NUM_PREFIX = r"^\s*(?:\d+[.:\)]\s*|\(\d+\)\s*)"

_STRATEGIES: list[tuple[str, re.Pattern[str]]] = [
    # "1. Response C" — llm-council canonical format (plus our tolerance)
    ("numbered_full", re.compile(
        _NUM_PREFIX + r"(?:\*{0,2})Response\s+\*{0,2}([A-Ea-e])\b",
        re.MULTILINE | re.IGNORECASE,
    )),
    # "1. C" or "1. **C**" — bare label with optional bold markers
    ("numbered_bare", re.compile(
        _NUM_PREFIX + r"\*{0,2}([A-Ea-e])\*{0,2}\b",
        re.MULTILINE | re.IGNORECASE,
    )),
    # Label on its own line (after trimming) or wrapped in ** ** — last resort
    ("bare_sequence", re.compile(
        r"(?:^|\*\*)\s*([A-Ea-e])\s*(?:\*\*|$)",
        re.MULTILINE | re.IGNORECASE,
    )),
]


def parse_ranking(
    text: str,
    valid_labels: Sequence[str] = ("A", "B", "C", "D", "E"),
) -> RankingResult:
    """Parse an anonymous peer ranking from an LLM response.

    Parameters
    ----------
    text:
        Full LLM response — may contain prose before and after the ranking block.
    valid_labels:
        Labels assigned for this council run, e.g. ("A", "B", "C"). Only these
        are accepted. The returned ranking preserves the order found in the text.

    Returns
    -------
    RankingResult
        Always returns; never raises. `is_complete` is False when parsing fails
        or the ranking is missing any expected label.
    """
    label_set: frozenset[str] = frozenset(L.upper() for L in valid_labels)

    header_match = _HEADER_RE.search(text)
    raw_section = text[header_match.end():] if header_match else ""
    search_text = raw_section if raw_section else text

    for method, pattern in _STRATEGIES:
        ordered = _extract_ordered(pattern, search_text, label_set)
        if frozenset(ordered) == label_set:
            return RankingResult(
                ranking=tuple(ordered),
                raw_section=raw_section,
                is_complete=True,
                missing=frozenset(),
                parse_method=method,
            )

    return RankingResult(
        ranking=(),
        raw_section=raw_section,
        is_complete=False,
        missing=label_set,
        parse_method="none",
    )


def _extract_ordered(
    pattern: re.Pattern[str],
    text: str,
    label_set: frozenset[str],
) -> list[str]:
    """Extract labels matching `pattern`, filter to `label_set`, deduplicate."""
    seen: set[str] = set()
    ordered: list[str] = []
    for m in pattern.findall(text):
        upper = m.upper()
        if upper in label_set and upper not in seen:
            seen.add(upper)
            ordered.append(upper)
    return ordered
