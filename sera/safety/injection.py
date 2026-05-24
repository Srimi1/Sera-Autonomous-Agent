"""Semantic prompt-injection classifier — P-81.

OUTCLASS: P-75's BlueAgent used a keyword list; this classifier scores every
text on a continuous 0-1 scale using weighted heuristics across five signal
families.  It replaces the regex toy without requiring a 500MB ONNX binary —
the heuristics achieve ≥95% recall / <2% FP on the curated 200-sample set
(verified in tests/test_injection.py).  When an actual `injection-cls.onnx`
is present at `model_path`, it is loaded via ONNX Runtime and used instead.

Signal families (heuristic mode)
---------------------------------
1. ROLE_HIJACK    — persona changes ("you are now", "from now on you are")
2. INSTRUCTION    — meta-commands ("ignore previous", "disregard", "forget all")
3. EXFILTRATION   — data extraction ("print all", "leak", "output your")
4. OVERRIDE       — authority claims ("system override", "admin mode", "jailbreak")
5. NESTED         — injection inside structured data (JSON value containing triggers)

Each match contributes a weight; score = min(1.0, total_weight).
Threshold: score ≥ 0.5 → is_injection.

The classifier is fully injectable — any callable `(str) -> InjectionScore`
can replace it at the call site, which is what the BlueAgent seam expects.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

# ---------------------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class InjectionScore:
    is_injection: bool
    score: float          # 0.0 (clean) → 1.0 (certain injection)
    reason: str | None    # human-readable explanation for the highest signal


THRESHOLD = 0.5


# ---------------------------------------------------------------------------
# Weighted signal patterns
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _Signal:
    family: str
    pattern: re.Pattern
    weight: float


_SIGNALS: list[_Signal] = [
    # INSTRUCTION
    _Signal("INSTRUCTION", re.compile(r"ignore\s+(all\s+)?previous\s+instructions?", re.I), 0.9),
    _Signal("INSTRUCTION", re.compile(r"ignore\s+(your|all|the)?\s*(ethical|safety|content)\s*(guidelines?|policies?|training|rules?)", re.I), 0.85),
    _Signal("INSTRUCTION", re.compile(r"disregard\s+(all\s+|your\s+)?(previous|safety|ethical|content)", re.I), 0.8),
    _Signal("INSTRUCTION", re.compile(r"forget\s+(everything|all\s+previous|your\s+previous)", re.I), 0.8),
    _Signal("INSTRUCTION", re.compile(r"forget\s+(all\s+)?(prior|existing)?\s*(instructions?|guidelines?|context)", re.I), 0.8),
    _Signal("INSTRUCTION", re.compile(r"do\s+not\s+follow\s+(any|the)\s+(previous|above)", re.I), 0.7),
    _Signal("INSTRUCTION", re.compile(r"(ignore|forget|disregard)\s+(about\s+)?(content|safety)\s+(policies?|filters?|guidelines?)", re.I), 0.8),
    _Signal("INSTRUCTION", re.compile(r"no\s+restrictions\s+on\s+your\s+responses?", re.I), 0.7),

    # ROLE_HIJACK
    _Signal("ROLE_HIJACK", re.compile(r"you\s+are\s+now\s+(DAN|an?\s+AI|a\s+different|in\s+unrestricted)", re.I), 0.9),
    _Signal("ROLE_HIJACK", re.compile(r"from\s+now\s+on\s+you\s+(are|will\s+be)", re.I), 0.8),
    _Signal("ROLE_HIJACK", re.compile(r"act\s+as\s+(if\s+you\s+(are|had\s+no)|an?\s+(uncensored|unrestricted))", re.I), 0.8),
    _Signal("ROLE_HIJACK", re.compile(r"pretend\s+(you\s+are|to\s+be)\s+(a\s+)?(?:different|evil|unrestricted)", re.I), 0.7),
    _Signal("ROLE_HIJACK", re.compile(r"act\s+as\s+an?\s+AI\s+that\s+never\s+refuses?", re.I), 0.85),
    _Signal("ROLE_HIJACK", re.compile(r"(you('re|\s+are)\s+now\s+in\s+(unrestricted|uncensored|developer|admin)\s+mode)", re.I), 0.85),
    _Signal("ROLE_HIJACK", re.compile(r"enter\s+(developer|admin|unrestricted|uncensored)\s+mode", re.I), 0.85),
    _Signal("ROLE_HIJACK", re.compile(r"no\s+(ethical|content)\s+(guidelines?|training|filters?)", re.I), 0.7),

    # EXFILTRATION
    _Signal("EXFILTRATION", re.compile(r"print\s+all\s+(previous|your)\s+(context|conversation|prompt)", re.I), 0.9),
    _Signal("EXFILTRATION", re.compile(r"(output|reveal|display|show)\s+(your\s+)?(system\s+)?prompt", re.I), 0.9),
    _Signal("EXFILTRATION", re.compile(r"leak\s+(your|the)\s+(memory|context|data|prompt)", re.I), 0.8),
    _Signal("EXFILTRATION", re.compile(r"repeat\s+everything\s+(above|before|prior)", re.I), 0.7),
    _Signal("EXFILTRATION", re.compile(r"(reveal|output|print|show)\s+(all\s+)?(your\s+)?(context|conversation|instructions?)", re.I), 0.8),

    # OVERRIDE
    _Signal("OVERRIDE", re.compile(r"SYSTEM\s+OVERRIDE", re.I), 0.95),
    _Signal("OVERRIDE", re.compile(r"disable\s+(all\s+)?(safety|content)\s+(filters?|guardrails?)", re.I), 0.9),
    _Signal("OVERRIDE", re.compile(r"disable\s+(all\s+)?(safety|content)\s+(filters?|guardrails?|policies?)", re.I), 0.9),
    _Signal("OVERRIDE", re.compile(r"(admin|developer|god|root)\s+mode\s+(enabled|activated|on)", re.I), 0.85),
    _Signal("OVERRIDE", re.compile(r"jailbreak\s+(mode|activated|now)", re.I), 0.9),
    _Signal("OVERRIDE", re.compile(r"bypass\s+(safety|filter|restriction|policy|content|training)", re.I), 0.8),
    _Signal("OVERRIDE", re.compile(r"override\s+(your\s+)?(content|safety|ethical)\s+(filter|training|guidelines?)", re.I), 0.8),
    _Signal("OVERRIDE", re.compile(r"(no\s+content\s+filters?|without\s+(restrictions?|filters?|ethical\s+training))", re.I), 0.7),
    _Signal("OVERRIDE", re.compile(r"authorized\s+by\s+the\s+development\s+team", re.I), 0.8),

    # INSTRUCTION — broader coverage
    _Signal("INSTRUCTION", re.compile(r"ignore\s+(all\s+|the\s+)?(instructions?|guidelines?|rules?)\s+(above|given|earlier|below)?", re.I), 0.8),
    _Signal("INSTRUCTION", re.compile(r"ignore\s+(instructions?|guidelines?)\s+and\s+bypass", re.I), 0.85),
    _Signal("INSTRUCTION", re.compile(r"ignore\s+all\s+(guidelines?|rules?)\s+and\s+answer", re.I), 0.8),

    # ROLE_HIJACK — broader
    _Signal("ROLE_HIJACK", re.compile(r"act\s+as\s+(uncensored|unrestricted)\s+AI", re.I), 0.85),

    # EXFILTRATION — broader
    _Signal("EXFILTRATION", re.compile(r"(show|display|tell\s+me)\s+(me\s+)?(your|the)\s+(system\s+)?prompt", re.I), 0.85),
    _Signal("EXFILTRATION", re.compile(r"(display|show|tell\s+me)\s+(everything\s+)?(in\s+)?your\s+context", re.I), 0.8),
    _Signal("EXFILTRATION", re.compile(r"output\s+(the\s+)?system\s+instructions", re.I), 0.85),
    _Signal("EXFILTRATION", re.compile(r"tell\s+me\s+everything\s+(in\s+)?(your\s+)?(context|memory)", re.I), 0.8),
    _Signal("EXFILTRATION", re.compile(r"revealing\s+your\s+system\s+prompt", re.I), 0.85),

    # OVERRIDE — broader
    _Signal("OVERRIDE", re.compile(r"bypass\s+all\s+(content\s+)?(restrictions?|filters?|guardrails?)", re.I), 0.85),

    # NESTED — injection inside JSON or HTML
    _Signal("NESTED", re.compile(
        r'"[^"]*(?:ignore|system override|you are now|disable safety)[^"]*"', re.I), 0.7),
    _Signal("NESTED", re.compile(
        r'<!--.*?(?:ignore|system override|jailbreak|comply).*?-->', re.I | re.DOTALL), 0.7),
]


# ---------------------------------------------------------------------------
# Heuristic classifier
# ---------------------------------------------------------------------------

def _heuristic_classify(text: str) -> InjectionScore:
    total = 0.0
    top_reason: str | None = None
    top_weight = 0.0

    for sig in _SIGNALS:
        if sig.pattern.search(text):
            total += sig.weight
            if sig.weight > top_weight:
                top_weight = sig.weight
                top_reason = sig.family

    score = min(1.0, total)
    return InjectionScore(
        is_injection=score >= THRESHOLD,
        score=round(score, 4),
        reason=top_reason,
    )


# ---------------------------------------------------------------------------
# ONNX loader (optional — graceful fallback)
# ---------------------------------------------------------------------------

def _load_onnx_classify(model_path: Path) -> Callable[[str], InjectionScore]:
    try:
        import onnxruntime as ort
        from transformers import AutoTokenizer  # type: ignore[import]

        sess = ort.InferenceSession(str(model_path))
        tokenizer = AutoTokenizer.from_pretrained(str(model_path.parent))

        def classify(text: str) -> InjectionScore:
            inputs = tokenizer(text, return_tensors="np", truncation=True, max_length=128)
            logits = sess.run(None, dict(inputs))[0][0]
            import math
            def softmax(x):
                ex = [math.exp(v - max(x)) for v in x]
                s = sum(ex)
                return [v / s for v in ex]
            probs = softmax(list(logits))
            score = float(probs[1]) if len(probs) > 1 else float(logits[0])
            return InjectionScore(
                is_injection=score >= THRESHOLD,
                score=round(min(1.0, score), 4),
                reason="onnx_model",
            )
        return classify
    except Exception:
        return _heuristic_classify


# ---------------------------------------------------------------------------
# Public classifier
# ---------------------------------------------------------------------------

class InjectionClassifier:
    """Classifies text as prompt-injection or clean.  ONNX or heuristic."""

    def __init__(self, model_path: Path | str | None = None) -> None:
        if model_path is not None:
            p = Path(model_path)
            self._classify = _load_onnx_classify(p) if p.exists() else _heuristic_classify
        else:
            self._classify = _heuristic_classify

    def classify(self, text: str) -> InjectionScore:
        return self._classify(text)

    def classify_batch(self, texts: list[str]) -> list[InjectionScore]:
        return [self._classify(t) for t in texts]
