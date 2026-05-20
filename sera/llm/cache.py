"""Prompt-cache stability for Anthropic.

Freeze-at-start: a session's system prompt is hashed and stored on first turn.
Subsequent turns of the same session restore the verbatim prompt, so the
Anthropic prompt cache keeps hitting across the session's lifetime.

`apply_cache_control_anthropic` returns the (system_blocks, messages) pair with
`cache_control: {"type": "ephemeral"}` markers placed on the system block and
the trailing tool_result blocks. The last-3 rolling window keeps the cache
breakpoint count under Anthropic's per-request cap (4) while still covering
the freshly-grown tail of the conversation.

Outclass claim: rivals (Hermes, OpenHuman, OpenClaw, llm-council) either
re-render the system prompt every turn or skip cache_control on tool results
entirely. Sera locks at start AND tags the rolling tail.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from sera.memory.session import Session

ANTHROPIC_TOOL_RESULT_CACHE_WINDOW = 3
"""Number of trailing tool_result blocks to mark cache_control on.

Anthropic allows up to 4 cache breakpoints per request. Reserving 1 for the
system block and 3 for the most recent tool turns keeps every breakpoint
contributing to genuinely-stable prefixes.
"""

_EPHEMERAL: dict[str, str] = {"type": "ephemeral"}


class FrozenPromptMismatch(RuntimeError):
    """Stored prompt hash doesn't match the on-disk prompt.

    Indicates DB corruption or external tampering — the session is unsafe to
    resume because the cached prefix would silently diverge from what the
    model actually sees.
    """


@dataclass(frozen=True)
class CacheUsage:
    """Per-turn token accounting reported by Anthropic's usage block."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0

    @property
    def total_input(self) -> int:
        return (
            self.input_tokens
            + self.cache_read_input_tokens
            + self.cache_creation_input_tokens
        )

    @property
    def cache_hit_ratio(self) -> float:
        denom = self.total_input
        return self.cache_read_input_tokens / denom if denom else 0.0


def hash_prompt(prompt: str) -> str:
    """Stable digest used to detect tampering across reloads."""
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def freeze_system_prompt(session: Session, prompt: str) -> str:
    """Persist the system prompt at session start; return the frozen value.

    First call on a session writes the prompt + hash to the sessions row.
    Later calls return the stored prompt verbatim — the `prompt` argument is
    ignored on resume so the cached prefix stays bit-identical.

    Raises FrozenPromptMismatch if a stored hash exists but does not match
    the stored prompt body (DB tampering / partial write).
    """
    row = session.conn.execute(
        "SELECT system_prompt, system_prompt_hash FROM sessions WHERE id = ?",
        (session.id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"session {session.id} not found")

    stored_prompt = row["system_prompt"]
    stored_hash = row["system_prompt_hash"]

    if stored_prompt is not None and stored_hash is not None:
        if hash_prompt(stored_prompt) != stored_hash:
            raise FrozenPromptMismatch(
                f"system prompt hash mismatch for session {session.id}"
            )
        return stored_prompt

    digest = hash_prompt(prompt)
    session.conn.execute(
        "UPDATE sessions SET system_prompt = ?, system_prompt_hash = ? WHERE id = ?",
        (prompt, digest, session.id),
    )
    session.conn.commit()
    return prompt


def apply_cache_control_anthropic(
    system: str,
    messages: list[dict[str, Any]],
    *,
    window: int = ANTHROPIC_TOOL_RESULT_CACHE_WINDOW,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (system_blocks, messages) with ephemeral cache_control markers.

    - system: wrapped as a single text block tagged ephemeral.
    - messages: deep-copied; the last `window` user messages whose content is
      a list of tool_result blocks get cache_control on their final block.

    Anthropic SDK consumes cache_control on the last block within a content
    list to mark "everything up to and including this point is cacheable".
    """
    system_blocks: list[dict[str, Any]] = [
        {"type": "text", "text": system, "cache_control": dict(_EPHEMERAL)}
    ]

    out: list[dict[str, Any]] = []
    for m in messages:
        content = m.get("content")
        if isinstance(content, list):
            out.append({**m, "content": [dict(b) for b in content]})
        else:
            out.append(dict(m))

    tool_result_indices: list[int] = []
    for i, m in enumerate(out):
        if m.get("role") != "user":
            continue
        content = m.get("content")
        if not isinstance(content, list):
            continue
        if any(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in content
        ):
            tool_result_indices.append(i)

    for i in tool_result_indices[-window:]:
        blocks = out[i]["content"]
        for block in reversed(blocks):
            if isinstance(block, dict):
                block["cache_control"] = dict(_EPHEMERAL)
                break

    return system_blocks, out


def parse_anthropic_usage(usage: Any) -> CacheUsage:
    """Read a CacheUsage out of an Anthropic `usage` object or dict.

    The streaming `get_final_message()` returns a pydantic object; the REST
    API returns a dict. Both expose the same field names — handle both shapes
    with getattr-then-getitem fallback.
    """
    if usage is None:
        return CacheUsage()

    def _g(name: str) -> int:
        v = getattr(usage, name, None)
        if v is None and isinstance(usage, dict):
            v = usage.get(name)
        return int(v or 0)

    return CacheUsage(
        input_tokens=_g("input_tokens"),
        output_tokens=_g("output_tokens"),
        cache_read_input_tokens=_g("cache_read_input_tokens"),
        cache_creation_input_tokens=_g("cache_creation_input_tokens"),
    )
