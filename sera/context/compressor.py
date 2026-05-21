"""Mid-turn context compaction.

When the OpenAI-style message list approaches the model's context budget,
collapse the middle into one assistant turn that *describes* what happened
rather than re-emitting it. Last K turns are kept verbatim so the model has
fresh state to act on.

Outclass over Hermes lineage:
  * "Remaining Work" framing, not "Next Steps" — the LLM treats this as
    reference, not as instructions to execute.
  * Fence prefix `[CONTEXT COMPACTION — REFERENCE ONLY]` so a downstream
    scrubber can detect and refuse to act on forged compaction tags.
  * Tail protected by tokens, not message count.
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from sera.context.tokens import estimate_messages

log = logging.getLogger("sera.context.compressor")

FENCE = "[CONTEXT COMPACTION — REFERENCE ONLY]"
SUMMARY_PROMPT = """You are condensing earlier conversation context for an autonomous agent named Sera.

Output structure (in this exact order, including the literal fence line):

[CONTEXT COMPACTION — REFERENCE ONLY]

## Remaining Work
- bullet list of unfinished tasks the user is tracking
- if none, write "None pending."

## Recent Decisions
- key choices already locked in, one line each, with the reason

## Open Threads
- side topics the user may return to

Rules:
- Use "Remaining Work" not "Next Steps" — this is reference, not an instruction.
- Reference past tool calls by name plus a short result summary; never paste raw tool output.
- No new tool calls. No questions. No apologies. No new claims.
- Maximum 800 words.
"""


SummariseFn = Callable[[list[dict[str, Any]]], Awaitable[str]]


async def compact_session(
    messages: list[dict[str, Any]],
    *,
    summarise: SummariseFn,
    budget_tokens: int,
    target_ratio: float = 0.8,
    tail_ratio: float = 0.3,
    min_messages_to_compact: int = 8,
) -> list[dict[str, Any]]:
    """Return a possibly-compacted view of `messages` for one LLM call.

    Pure function: does not mutate `messages` or the DB.

    Args:
        messages: OpenAI-style message list (with role, content, tool_calls).
        summarise: async fn that takes the middle slice + returns a summary string.
        budget_tokens: model context budget.
        target_ratio: compact when current usage > target_ratio * budget. Default 0.8.
        tail_ratio: fraction of budget reserved for verbatim tail. Default 0.3.
        min_messages_to_compact: don't compact short sessions even if over budget.
    """
    if len(messages) < min_messages_to_compact:
        return messages

    current = estimate_messages(messages)
    threshold = int(budget_tokens * target_ratio)
    if current <= threshold:
        return messages

    # Split system head + body.
    head: list[dict[str, Any]] = []
    body_start = 0
    for i, m in enumerate(messages):
        if m.get("role") == "system":
            head.append(m)
            body_start = i + 1
        else:
            break
    body = messages[body_start:]

    if len(body) < min_messages_to_compact:
        return messages

    # Walk from the tail backward, keep as many messages as fit in tail budget.
    tail_budget = int(budget_tokens * tail_ratio)
    tail: list[dict[str, Any]] = []
    tail_tokens = 0
    for m in reversed(body):
        cost = estimate_messages([m])
        if tail_tokens + cost > tail_budget and tail:
            break
        tail.insert(0, m)
        tail_tokens += cost

    # Tail-orphan repair (two cases):
    #  (a) A leading `tool` message has no matching assistant in the tail because
    #      the assistant got cut into the middle slice. Drop it.
    #  (b) A leading `assistant` has `tool_calls` whose result messages got cut
    #      into the middle slice. Either pull the tools forward into the tail or
    #      strip the tool_calls field — we strip, because pulling forward would
    #      blow the tail budget unboundedly.
    while tail and tail[0].get("role") == "tool":
        tail.pop(0)
    if (
        tail
        and tail[0].get("role") == "assistant"
        and tail[0].get("tool_calls")
    ):
        wanted_ids = {tc["id"] for tc in tail[0]["tool_calls"]}
        tail_tool_ids = {
            m.get("tool_call_id") for m in tail if m.get("role") == "tool"
        }
        if not wanted_ids.issubset(tail_tool_ids):
            # Orphan: corresponding tool results are gone. Strip tool_calls so the
            # provider doesn't 400 on dangling references.
            tail[0] = {**tail[0], "tool_calls": []}
            if not tail[0].get("content"):
                tail[0]["content"] = "[earlier reasoning compacted]"

    if not tail:
        # Tail budget too small; keep last 3 messages regardless.
        tail = body[-3:]

    middle = body[: len(body) - len(tail)]
    if not middle:
        return messages

    log.info(
        "compact: budget=%d current=%d threshold=%d middle=%d tail=%d",
        budget_tokens, current, threshold, len(middle), len(tail),
    )

    summary_text = await summarise(middle)
    if not summary_text.startswith(FENCE):
        summary_text = f"{FENCE}\n\n{summary_text.strip()}"

    summary_msg = {
        "role": "assistant",
        "content": summary_text,
    }

    return [*head, summary_msg, *tail]


def build_summarise_call(
    llm,
    *,
    system_prompt: str = SUMMARY_PROMPT,
) -> SummariseFn:
    """Adapter: wrap an LLM (Sera's protocol) as a SummariseFn for compact_session."""

    async def _summarise(middle: list[dict[str, Any]]) -> str:
        condensed = "\n".join(_render_turn(m) for m in middle)
        # One-shot stream, no tools.
        messages = [
            {"role": "user", "content": f"Here is the conversation slice to condense:\n\n{condensed}"}
        ]
        out_parts: list[str] = []
        async for chunk in llm.stream(messages=messages, tools=None, system=system_prompt):
            if chunk.delta_text:
                out_parts.append(chunk.delta_text)
            if chunk.finish_reason:
                break
        return "".join(out_parts).strip()

    return _summarise


def _render_turn(m: dict[str, Any]) -> str:
    role = m.get("role", "?")
    content = m.get("content")
    if isinstance(content, list):
        content = " ".join(
            p.get("text", "") for p in content if isinstance(p, dict)
        )
    tcs = m.get("tool_calls") or []
    if tcs:
        names = [f"{tc.get('function', {}).get('name', '?')}" for tc in tcs]
        return f"[{role}] (tool_calls: {', '.join(names)}) {content or ''}"
    if role == "tool":
        head = (content or "").splitlines()[0][:200] if content else ""
        return f"[tool:{m.get('name', '?')}] {head}"
    return f"[{role}] {content or ''}"
