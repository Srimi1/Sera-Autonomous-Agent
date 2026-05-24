"""Blue agent — scans sessions, skills, and memory for adversarial payloads.

Uses a simple keyword classifier by default.  P-81 (semantic prompt-injection
classifier) will replace the `classifier` callable with a trained model;
the seam is identical — same input/output contract.

Classifier contract
-------------------
    classify(text: str) -> ClassifyResult
    ClassifyResult.is_injection: bool
    ClassifyResult.kind: str | None   # e.g. "IGNORE", "ROLE_SWITCH"
    ClassifyResult.confidence: float  # 0.0-1.0
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

# ---------------------------------------------------------------------------
# Classifier contract + built-in keyword classifier (P-81 stub)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ClassifyResult:
    is_injection: bool
    kind: str | None = None
    confidence: float = 1.0


_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"ignore\s+previous\s+instructions", re.I), "IGNORE"),
    (re.compile(r"you\s+are\s+now\s+(DAN|a\s+different\s+AI)", re.I), "ROLE_SWITCH"),
    (re.compile(r"print\s+all\s+previous\s+context", re.I), "EXFIL"),
    (re.compile(r"SYSTEM\s+OVERRIDE", re.I), "OVERRIDE"),
    (re.compile(r"disable\s+safety\s+filter", re.I), "OVERRIDE"),
]


def _keyword_classifier(text: str) -> ClassifyResult:
    for pattern, kind in _PATTERNS:
        if pattern.search(text):
            return ClassifyResult(is_injection=True, kind=kind, confidence=0.95)
    return ClassifyResult(is_injection=False)


Classifier = Callable[[str], ClassifyResult]


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Finding:
    location: str          # "tool_result", "skill_body", "memory_chunk", "message"
    text_excerpt: str      # first 200 chars of the flagged text
    kind: str | None
    confidence: float


@dataclass
class BlueRun:
    findings: list[Finding] = field(default_factory=list)

    @property
    def caught(self) -> int:
        return len(self.findings)

    @property
    def any_found(self) -> bool:
        return bool(self.findings)


# ---------------------------------------------------------------------------
# Blue agent
# ---------------------------------------------------------------------------

class BlueAgent:
    """Scans structures for adversarial payloads.  Classifier is injectable."""

    def __init__(self, classifier: Classifier | None = None) -> None:
        self._classify = classifier or _keyword_classifier

    def scan_text(self, text: str, location: str) -> Finding | None:
        result = self._classify(text)
        if result.is_injection:
            return Finding(
                location=location,
                text_excerpt=text[:200],
                kind=result.kind,
                confidence=result.confidence,
            )
        return None

    def scan_messages(self, messages: list[dict[str, Any]]) -> list[Finding]:
        findings: list[Finding] = []
        for m in messages:
            content = str(m.get("content") or "")
            if not content:
                continue
            role = m.get("role", "")
            loc = "tool_result" if role == "tool" else f"message[{role}]"
            f = self.scan_text(content, loc)
            if f:
                findings.append(f)
        return findings

    def scan_skill(self, skill: dict[str, Any]) -> Finding | None:
        body = str(skill.get("body") or "")
        return self.scan_text(body, "skill_body")

    def scan_chunk(self, chunk: dict[str, Any]) -> Finding | None:
        content = str(chunk.get("content") or "")
        return self.scan_text(content, "memory_chunk")

    def run(
        self,
        *,
        messages: list[dict[str, Any]] | None = None,
        skills: list[dict[str, Any]] | None = None,
        chunks: list[dict[str, Any]] | None = None,
    ) -> BlueRun:
        result = BlueRun()
        if messages:
            result.findings.extend(self.scan_messages(messages))
        if skills:
            for s in skills:
                f = self.scan_skill(s)
                if f:
                    result.findings.append(f)
        if chunks:
            for c in chunks:
                f = self.scan_chunk(c)
                if f:
                    result.findings.append(f)
        return result
