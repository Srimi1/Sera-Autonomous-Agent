"""PII detection + consent gate.

Two-layer: regex detectors for the obvious kinds (SSN, credit card, email,
phone, IPv4, API tokens) plus an optional Presidio bridge for spaCy-NER-grade
detection when the library is installed. Sera ships with regex only —
Presidio is opt-in via `pip install sera[privacy]`.

Outclass: most rivals scrub at output time (or not at all). Sera tags chunks
at ingest and gates retrieval behind explicit `consent=True`. PII never
slips into an agent's working context without a deliberate per-query toggle.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Iterable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PIIMatch:
    """One detected PII span. `kind` is the canonical tag we persist."""

    kind: str
    start: int
    end: int
    text: str


# ─── Regex detectors ───────────────────────────────────────────────


# Order matters: longer/more-specific patterns come first so they win when
# spans overlap. Each tuple is (kind, compiled-pattern).
_DETECTORS: list[tuple[str, re.Pattern[str]]] = [
    # Provider secret tokens — most specific first.
    ("anthropic_key", re.compile(r"sk-ant-(?:api|oat)\d*-[A-Za-z0-9_\-]{20,}")),
    ("openai_key", re.compile(r"sk-proj-[A-Za-z0-9_\-]{20,}")),
    ("openai_key", re.compile(r"sk-[A-Za-z0-9]{20,}")),
    ("github_pat", re.compile(r"ghp_[A-Za-z0-9]{20,}")),
    ("github_oauth", re.compile(r"gho_[A-Za-z0-9]{20,}")),
    ("github_server", re.compile(r"ghs_[A-Za-z0-9]{20,}")),
    ("github_pat", re.compile(r"github_pat_[A-Za-z0-9_]{40,}")),
    ("slack_token", re.compile(r"xox[abprs]-[A-Za-z0-9\-]{10,}")),
    ("aws_access_key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("aws_session_key", re.compile(r"ASIA[0-9A-Z]{16}")),
    # SSN (US format). 3-2-4 with dashes.
    ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    # Credit-card-shaped digit runs — Luhn check happens in `_luhn_filter`.
    (
        "credit_card",
        re.compile(r"\b(?:\d[ -]?){13,19}\b"),
    ),
    # Email — RFC-lenient but good enough for ingest tagging.
    (
        "email",
        re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"),
    ),
    # Phone — international + US shapes. Permissive on punctuation.
    (
        "phone",
        re.compile(
            r"(?<!\w)(?:\+?\d{1,3}[ .\-]?)?(?:\(\d{2,4}\)[ .\-]?|\d{2,4}[ .\-])"
            r"\d{3,4}[ .\-]?\d{3,4}(?!\w)"
        ),
    ),
    # IPv4 dotted-quad.
    ("ipv4", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
]


def _luhn_ok(digits: str) -> bool:
    """Standard Luhn checksum. `digits` is digits only (no spaces / dashes)."""
    if not digits.isdigit() or not (13 <= len(digits) <= 19):
        return False
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


# ─── Detection ─────────────────────────────────────────────────────


def detect(text: str) -> list[PIIMatch]:
    """Run every regex detector + Luhn filter and return non-overlapping matches.

    Overlaps are resolved by first-match-wins ordering (longer / more-specific
    detectors appear earlier in `_DETECTORS`). Empty input returns `[]`.
    """
    if not text:
        return []
    raw: list[PIIMatch] = []
    for kind, pattern in _DETECTORS:
        for m in pattern.finditer(text):
            span_text = m.group(0)
            if kind == "credit_card":
                digits = re.sub(r"\D", "", span_text)
                if not _luhn_ok(digits):
                    continue
            raw.append(
                PIIMatch(kind=kind, start=m.start(), end=m.end(), text=span_text)
            )
    if not raw:
        return []
    # Sort by start ascending, then by length descending so a more-specific
    # earlier-detector match wins on tie.
    raw.sort(key=lambda m: (m.start, -(m.end - m.start)))
    out: list[PIIMatch] = []
    last_end = -1
    for pm in raw:
        if pm.start < last_end:
            continue  # overlap with prior, longer match
        out.append(pm)
        last_end = pm.end
    return out


def has_pii(text: str) -> bool:
    """Cheap boolean form — short-circuits at the first detector hit."""
    if not text:
        return False
    for kind, pattern in _DETECTORS:
        for m in pattern.finditer(text):
            if kind == "credit_card":
                if not _luhn_ok(re.sub(r"\D", "", m.group(0))):
                    continue
            return True
    return False


def pii_kinds(text: str) -> list[str]:
    """Unique tag list (deduped, deterministic order) for ingest persistence."""
    seen: list[str] = []
    for m in detect(text):
        if m.kind not in seen:
            seen.append(m.kind)
    return seen


def redact_pii(text: str, *, marker: str = "<redacted:{kind}>") -> str:
    """Rewrite `text` with detected spans replaced by a marker.

    The default marker keeps the kind tag inline so logs / tool outputs
    stay debuggable without revealing the value. Pass a custom format
    string for stricter contexts (`marker="<redacted>"`).
    """
    matches = detect(text)
    if not matches:
        return text
    out: list[str] = []
    cursor = 0
    for m in matches:
        out.append(text[cursor : m.start])
        out.append(marker.format(kind=m.kind))
        cursor = m.end
    out.append(text[cursor:])
    return "".join(out)


# ─── Optional Presidio adapter ─────────────────────────────────────


def _try_load_presidio():
    """Return a Presidio analyzer instance, or None if the lib is absent."""
    try:
        from presidio_analyzer import AnalyzerEngine

        return AnalyzerEngine()
    except Exception:  # noqa: BLE001 — install path is best-effort
        return None


_PRESIDIO_ANALYZER = None
_PRESIDIO_CHECKED = False


def detect_with_presidio(text: str, *, language: str = "en") -> list[PIIMatch]:
    """Use Presidio if installed; otherwise fall back to regex.

    Presidio's entity labels are remapped onto our canonical kinds so the
    downstream consent gate stays uniform across detector backends.
    """
    global _PRESIDIO_ANALYZER, _PRESIDIO_CHECKED
    if not _PRESIDIO_CHECKED:
        _PRESIDIO_CHECKED = True
        _PRESIDIO_ANALYZER = _try_load_presidio()
        if _PRESIDIO_ANALYZER is None:
            logger.info(
                "presidio-analyzer not installed; "
                "falling back to regex PII detection."
            )
    if _PRESIDIO_ANALYZER is None:
        return detect(text)

    label_map = {
        "US_SSN": "ssn",
        "CREDIT_CARD": "credit_card",
        "EMAIL_ADDRESS": "email",
        "PHONE_NUMBER": "phone",
        "IP_ADDRESS": "ipv4",
        "US_BANK_NUMBER": "bank_account",
        "IBAN_CODE": "iban",
    }
    raw = _PRESIDIO_ANALYZER.analyze(text=text, language=language)
    out: list[PIIMatch] = []
    for r in raw:
        kind = label_map.get(r.entity_type, r.entity_type.lower())
        out.append(
            PIIMatch(kind=kind, start=r.start, end=r.end, text=text[r.start : r.end])
        )
    out.sort(key=lambda m: (m.start, -(m.end - m.start)))
    return out


def known_kinds() -> Iterable[str]:
    """Set of canonical tag strings produced by the regex backend."""
    return {k for k, _ in _DETECTORS}
