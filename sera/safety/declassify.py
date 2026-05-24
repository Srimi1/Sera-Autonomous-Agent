"""Privacy declassifier — bulk PII scrub of session/audit logs (P-88).

OUTCLASS: Rivals scrub at output time or not at all.  Sera declassifies the
*entire* persisted log in one pass, produces a before/after diff of every
span that was redacted, and returns a redacted copy without mutating the
original.  Apply before sharing logs with anyone.

Usage
-----
    from sera.safety.declassify import Declassifier
    dc = Declassifier()
    result = dc.run(log_text)
    print(result.summary())   # how many spans were scrubbed
    open("clean.log", "w").write(result.redacted)
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from sera.memory.privacy import PIIMatch, detect as detect_pii, redact_pii
from sera.safety.redact import redact as redact_secrets


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RedactedSpan:
    line_no: int        # 1-based
    original: str       # full original line
    redacted: str       # full redacted line
    matches: tuple[PIIMatch, ...]


@dataclass
class DeclassifyResult:
    redacted: str
    spans: list[RedactedSpan] = field(default_factory=list)

    @property
    def n_lines_changed(self) -> int:
        return len(self.spans)

    @property
    def n_spans(self) -> int:
        return sum(len(s.matches) for s in self.spans)

    def summary(self) -> str:
        return (
            f"Declassified: {self.n_lines_changed} lines changed, "
            f"{self.n_spans} PII spans redacted."
        )

    def diff_lines(self) -> list[str]:
        """Unified-style diff entries for changed lines only."""
        out: list[str] = []
        for span in self.spans:
            out.append(f"@@ line {span.line_no} @@")
            out.append(f"-{span.original}")
            out.append(f"+{span.redacted}")
        return out


# ---------------------------------------------------------------------------
# Core declassifier
# ---------------------------------------------------------------------------

class Declassifier:
    """Walk any text log line-by-line and redact PII in every field."""

    def __init__(self, deep: bool = True) -> None:
        # deep=True: also parse JSON lines and recurse into string values
        self._deep = deep

    def run(self, text: str) -> DeclassifyResult:
        """Declassify `text`; return result with redacted copy and span list."""
        lines = text.splitlines(keepends=True)
        out_lines: list[str] = []
        spans: list[RedactedSpan] = []

        for i, line in enumerate(lines, start=1):
            clean, matches = self._process_line(line)
            out_lines.append(clean)
            if matches:
                spans.append(RedactedSpan(
                    line_no=i,
                    original=line.rstrip("\n"),
                    redacted=clean.rstrip("\n"),
                    matches=tuple(matches),
                ))

        return DeclassifyResult(redacted="".join(out_lines), spans=spans)

    def run_file(self, path: Any) -> DeclassifyResult:
        from pathlib import Path
        p = Path(path)
        return self.run(p.read_text(encoding="utf-8"))

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _process_line(self, line: str) -> tuple[str, list[PIIMatch]]:
        """Return (redacted_line, matches). Parses JSON if self._deep."""
        stripped = line.rstrip("\n\r")

        if self._deep and stripped.startswith("{"):
            try:
                obj = json.loads(stripped)
                clean_obj, matches = _redact_object(obj)
                # preserve trailing newline style
                suffix = line[len(stripped):]
                return json.dumps(clean_obj, separators=(",", ":"),
                                  ensure_ascii=False) + suffix, matches
            except (json.JSONDecodeError, ValueError):
                pass

        # Plain text fallback
        return self._redact_plain(line)

    def _redact_plain(self, line: str) -> tuple[str, list[PIIMatch]]:
        matches = detect_pii(line)
        if not matches:
            return line, []
        # Two-pass: privacy.py handles PII kinds; redact.py handles secrets
        clean = redact_pii(line)
        clean = redact_secrets(clean)
        return clean, matches


# ---------------------------------------------------------------------------
# Recursive object redactor
# ---------------------------------------------------------------------------

def _redact_object(obj: Any) -> tuple[Any, list[PIIMatch]]:
    """Recursively redact string values in a JSON object; collect matches."""
    all_matches: list[PIIMatch] = []

    if isinstance(obj, str):
        pii = detect_pii(obj)
        if pii:
            all_matches.extend(pii)
            clean = redact_secrets(redact_pii(obj))
            return clean, all_matches
        return obj, []

    if isinstance(obj, dict):
        out: dict = {}
        for k, v in obj.items():
            clean_v, m = _redact_object(v)
            out[k] = clean_v
            all_matches.extend(m)
        return out, all_matches

    if isinstance(obj, list):
        out_list: list = []
        for item in obj:
            clean_item, m = _redact_object(item)
            out_list.append(clean_item)
            all_matches.extend(m)
        return out_list, all_matches

    return obj, []
