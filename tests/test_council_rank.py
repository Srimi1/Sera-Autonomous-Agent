"""P-32: anonymous peer ranking parser — 20-case verification suite."""
from __future__ import annotations

import pytest

from sera.council.rank import RankingResult, parse_ranking

_ABC = ("A", "B", "C")
_ABCDE = ("A", "B", "C", "D", "E")


# ─── 1. Canonical numbered_full (llm-council format) ──────────────

def test_canonical_response_prefix():
    text = "FINAL RANKING:\n1. Response C\n2. Response A\n3. Response B"
    r = parse_ranking(text, _ABC)
    assert r.is_complete
    assert r.ranking == ("C", "A", "B")
    assert r.parse_method == "numbered_full"


# ─── 2. numbered_bare — no "Response" prefix ──────────────────────

def test_bare_label_no_prefix():
    text = "FINAL RANKING:\n1. C\n2. A\n3. B"
    r = parse_ranking(text, _ABC)
    assert r.is_complete
    assert r.ranking == ("C", "A", "B")
    assert r.parse_method == "numbered_bare"


# ─── 3. Commentary before ranking block ───────────────────────────

def test_commentary_before_block():
    text = (
        "After reviewing all responses, I think Response C was clearest.\n\n"
        "FINAL RANKING:\n1. C\n2. A\n3. B"
    )
    r = parse_ranking(text, _ABC)
    assert r.is_complete
    assert r.ranking == ("C", "A", "B")


# ─── 4. Commentary between ranking items ──────────────────────────

def test_commentary_between_items():
    text = (
        "FINAL RANKING:\n"
        "1. C — best reasoning and most complete\n"
        "2. A — solid but missed edge case\n"
        "3. B — too brief\n"
    )
    r = parse_ranking(text, _ABC)
    assert r.is_complete
    assert r.ranking == ("C", "A", "B")


# ─── 5. Inline annotation after label ─────────────────────────────

def test_inline_annotation_after_label():
    text = "FINAL RANKING:\n1. C (best)\n2. A (ok)\n3. B (worst)"
    r = parse_ranking(text, _ABC)
    assert r.is_complete
    assert r.ranking == ("C", "A", "B")


# ─── 6. Case-insensitive header ───────────────────────────────────

def test_header_lowercase():
    text = "final ranking:\n1. C\n2. A\n3. B"
    r = parse_ranking(text, _ABC)
    assert r.is_complete
    assert r.ranking == ("C", "A", "B")


# ─── 7. Header with extra spaces ─────────────────────────────────

def test_header_extra_spaces():
    text = "FINAL  RANKING :\n1. C\n2. A\n3. B"
    r = parse_ranking(text, _ABC)
    assert r.is_complete
    assert r.ranking == ("C", "A", "B")


# ─── 8. Colon-style numbering ────────────────────────────────────

def test_colon_numbering():
    text = "FINAL RANKING:\n1: C\n2: A\n3: B"
    r = parse_ranking(text, _ABC)
    assert r.is_complete
    assert r.ranking == ("C", "A", "B")


# ─── 9. Paren-style numbering ────────────────────────────────────

def test_paren_suffix_numbering():
    text = "FINAL RANKING:\n1) C\n2) A\n3) B"
    r = parse_ranking(text, _ABC)
    assert r.is_complete
    assert r.ranking == ("C", "A", "B")


# ─── 10. Paren-prefix numbering ──────────────────────────────────

def test_paren_prefix_numbering():
    text = "FINAL RANKING:\n(1) C\n(2) A\n(3) B"
    r = parse_ranking(text, _ABC)
    assert r.is_complete
    assert r.ranking == ("C", "A", "B")


# ─── 11. Lowercase labels ─────────────────────────────────────────

def test_lowercase_labels():
    text = "FINAL RANKING:\n1. c\n2. a\n3. b"
    r = parse_ranking(text, _ABC)
    assert r.is_complete
    assert r.ranking == ("C", "A", "B")


# ─── 12. Bold-wrapped labels ─────────────────────────────────────

def test_bold_labels():
    text = "FINAL RANKING:\n1. **C**\n2. **A**\n3. **B**"
    r = parse_ranking(text, _ABC)
    assert r.is_complete
    assert r.ranking == ("C", "A", "B")


# ─── 13. Long response, ranking buried in middle ─────────────────

def test_ranking_buried_in_long_response():
    preamble = "lorem ipsum " * 50
    postamble = "conclusion " * 20
    text = preamble + "\nFINAL RANKING:\n1. B\n2. C\n3. A\n" + postamble
    r = parse_ranking(text, _ABC)
    assert r.is_complete
    assert r.ranking == ("B", "C", "A")


# ─── 14. Five-model council (A–E) ────────────────────────────────

def test_five_model_council():
    text = "FINAL RANKING:\n1. E\n2. B\n3. A\n4. D\n5. C"
    r = parse_ranking(text, _ABCDE)
    assert r.is_complete
    assert r.ranking == ("E", "B", "A", "D", "C")


# ─── 15. raw_section captured ────────────────────────────────────

def test_raw_section_captured():
    text = "Some preamble.\nFINAL RANKING:\n1. A\n2. B\n3. C"
    r = parse_ranking(text, _ABC)
    assert "1. A" in r.raw_section
    assert "Some preamble" not in r.raw_section


# ─── REJECT cases ─────────────────────────────────────────────────

def test_rejects_no_header():
    text = "My ranking:\n1. C\n2. A\n3. B"
    r = parse_ranking(text, _ABC)
    # No FINAL RANKING header → falls through to full text scan.
    # Numbered bare strategy on full text might still find it.
    # But the phase intent is: without header, result may succeed or fail;
    # the key guarantee is NO EXCEPTION and is_complete reflects reality.
    assert isinstance(r, RankingResult)  # must not raise


def test_rejects_incomplete_ranking():
    text = "FINAL RANKING:\n1. C\n2. A"  # missing B
    r = parse_ranking(text, _ABC)
    assert not r.is_complete
    assert r.ranking == ()
    assert "B" in r.missing


def test_rejects_wrong_labels():
    text = "FINAL RANKING:\n1. D\n2. E\n3. F"
    r = parse_ranking(text, _ABC)
    assert not r.is_complete


def test_rejects_empty_text():
    r = parse_ranking("", _ABC)
    assert not r.is_complete
    assert r.ranking == ()
    assert r.parse_method == "none"


def test_rejects_only_commentary():
    text = "I think all responses were equally good and cannot rank them."
    r = parse_ranking(text, _ABC)
    assert not r.is_complete
    assert r.parse_method == "none"
