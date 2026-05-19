"""Secret + PII redaction.

Used everywhere a string crosses a trust boundary: tool output, traceback,
CLI echo, LLM-bound messages, persisted DB rows. Conservative: prefers
overly-cautious masking over leakage.
"""
from __future__ import annotations

import re

# Order matters: longer / more-specific patterns first.
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Anthropic + OpenAI keys (must come before generic sk- match).
    (re.compile(r"sk-ant-(?:api|oat)\d*-[A-Za-z0-9_\-]{20,}"), "<redacted:anthropic-key>"),
    (re.compile(r"sk-proj-[A-Za-z0-9_\-]{20,}"), "<redacted:openai-key>"),
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), "<redacted:openai-key>"),
    # GitHub PATs and tokens.
    (re.compile(r"ghp_[A-Za-z0-9]{20,}"), "<redacted:github-pat>"),
    (re.compile(r"gho_[A-Za-z0-9]{20,}"), "<redacted:github-oauth>"),
    (re.compile(r"ghs_[A-Za-z0-9]{20,}"), "<redacted:github-server>"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{40,}"), "<redacted:github-pat-finegrained>"),
    # Slack.
    (re.compile(r"xox[abprs]-[A-Za-z0-9\-]{10,}"), "<redacted:slack-token>"),
    # AWS access keys.
    (re.compile(r"AKIA[0-9A-Z]{16}"), "<redacted:aws-access-key>"),
    (re.compile(r"ASIA[0-9A-Z]{16}"), "<redacted:aws-session-key>"),
    # Bearer tokens in headers.
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9_\-\.]{20,}"), "Bearer <redacted>"),
    # Authorization header form.
    (re.compile(r"(?i)authorization:\s*[A-Za-z0-9_\-\.\s]{20,}"), "Authorization: <redacted>"),
    # Generic env-style assignments for sensitive names.
    (
        re.compile(
            r"(?i)(OPENAI_API_KEY|ANTHROPIC_API_KEY|AWS_SECRET_ACCESS_KEY|"
            r"GITHUB_TOKEN|TAVILY_API_KEY|HUGGINGFACE_TOKEN)\s*[=:]\s*\S+"
        ),
        r"\1=<redacted>",
    ),
    # Private key bodies.
    (
        re.compile(
            r"-----BEGIN (?:RSA |OPENSSH |EC |DSA |ENCRYPTED )?PRIVATE KEY-----.*?"
            r"-----END (?:RSA |OPENSSH |EC |DSA |ENCRYPTED )?PRIVATE KEY-----",
            re.DOTALL,
        ),
        "<redacted:private-key>",
    ),
    # JWT-shaped tokens (three base64url segments separated by dots).
    (
        re.compile(
            r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b"
        ),
        "<redacted:jwt>",
    ),
]


def redact(text: str) -> str:
    """Return a copy of `text` with known secret/credential patterns masked."""
    if not text:
        return text
    out = text
    for pat, repl in _PATTERNS:
        out = pat.sub(repl, out)
    return out


def has_secret(text: str) -> bool:
    """True if any redactable pattern fires. Cheap probe for tests/audit."""
    if not text:
        return False
    return any(pat.search(text) for pat, _ in _PATTERNS)
