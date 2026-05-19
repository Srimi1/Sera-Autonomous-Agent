"""Token estimation. Lazy tiktoken with chars/4 fallback."""
from __future__ import annotations

from typing import Any, Iterable

_ENCODER = None
_FALLBACK_RATIO = 4  # ~4 chars per token, conservative


def _encoder():
    """Lazy-load tiktoken's cl100k_base encoder. None if tiktoken absent."""
    global _ENCODER
    if _ENCODER is None:
        try:
            import tiktoken

            _ENCODER = tiktoken.get_encoding("cl100k_base")
        except Exception:  # noqa: BLE001 — offline, missing, network blocked
            _ENCODER = False  # sentinel for "tried, failed"
    return _ENCODER or None


def estimate(text: str) -> int:
    """Token count for a single string."""
    if not text:
        return 0
    enc = _encoder()
    if enc is not None:
        return len(enc.encode(text))
    return max(1, len(text) // _FALLBACK_RATIO)


# Per-message token cache. Messages are mutable dicts so identity is unstable
# across recreations; we key by (role, content-len, tool-call-count, name) plus
# a 64-bit hash of the content/args. Cheap on hits, ~1µs on miss vs ~75µs to
# re-tokenize, so the cache pays for itself after 1 lookup.
_msg_cache: dict[tuple, int] = {}
_MAX_CACHE = 4096


def _msg_key(m: dict[str, Any]) -> tuple:
    content = m.get("content")
    if isinstance(content, list):
        # Convert structured content to a stable string for hashing.
        content_repr = "".join(
            (p.get("text", "") if isinstance(p, dict) else str(p)) for p in content
        )
    else:
        content_repr = "" if content is None else str(content)
    tcs = m.get("tool_calls") or []
    tc_repr = "|".join(
        f"{(tc.get('function') or {}).get('name', '')}:{(tc.get('function') or {}).get('arguments', '')}"
        for tc in tcs
    )
    return (
        m.get("role"),
        m.get("name"),
        hash(content_repr),
        hash(tc_repr),
        len(content_repr),
    )


def _cost_for_message(m: dict[str, Any]) -> int:
    key = _msg_key(m)
    cached = _msg_cache.get(key)
    if cached is not None:
        return cached
    total = 4  # role + structural overhead
    content = m.get("content")
    if content:
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and "text" in part:
                    total += estimate(part["text"])
                elif isinstance(part, str):
                    total += estimate(part)
        else:
            total += estimate(str(content))
    for tc in m.get("tool_calls") or []:
        fn = tc.get("function") or {}
        total += estimate(fn.get("name", ""))
        args = fn.get("arguments", "")
        if not isinstance(args, str):
            args = str(args)
        total += estimate(args)
    if (name := m.get("name")):
        total += estimate(name)
    if len(_msg_cache) >= _MAX_CACHE:
        # Crude eviction: drop one arbitrary entry.
        _msg_cache.pop(next(iter(_msg_cache)))
    _msg_cache[key] = total
    return total


def estimate_messages(messages: Iterable[dict[str, Any]]) -> int:
    """Token count across an OpenAI-style messages list, including tool_calls.

    Uses a content-hash cache so the same message hit on subsequent calls is O(1).
    """
    return sum(_cost_for_message(m) for m in messages)
