"""ReAct agent loop. Heritage: hermes/agent/conversation_loop.py:526."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from sera.context.compressor import FENCE, build_summarise_call, compact_session
from sera.context.scrubber import StreamingContextScrubber, scrub
from sera.context.tokens import estimate_messages
from sera.llm.base import LLM, ContextOverflow
from sera.memory.session import Message, Session
from sera.safety.approval import ApprovalGate, AutoApproveGate
from sera.safety.redact import redact
from sera.tools.base import Permission, ToolCall, ToolContext
from sera.tools.dispatcher import execute as dispatch_execute
from sera.tools.registry import all_tools, get as get_tool

SYSTEM_PROMPT = (
    "You are Sera, an autonomous agent running locally on the user's machine.\n"
    "You have tools for files, shell, web search, and long-term memory.\n"
    "Be concise. Call tools when needed; otherwise answer directly.\n"
    "Always justify destructive shell commands before running them."
)

DEFAULT_MAX_ITERATIONS = 25


@dataclass
class TokenSink:
    """Where streamed assistant text gets written. Default: stdout."""

    on_text: Callable[[str], None]
    on_tool_start: Callable[[str, dict[str, Any]], None] = lambda n, a: None
    on_tool_end: Callable[[str, str], None] = lambda n, r: None


def _stdout_sink() -> TokenSink:
    import sys

    def write(t: str) -> None:
        sys.stdout.write(t)
        sys.stdout.flush()

    return TokenSink(on_text=write)


def _effective_permission(call: ToolCall) -> Permission:
    """Runtime permission. Tools with classifiers override the static base tier.

    For shell_run (base = DANGEROUS), the classifier may downgrade safe commands
    (ls, git status, cat, …) to EXECUTE. For tools without a classifier the
    static base is authoritative.
    """
    tool = get_tool(call.name)
    if tool is None:
        return Permission.NONE
    if call.name == "shell_run":
        from sera.tools.impl.shell_run import classify

        return classify(call.arguments.get("command", ""))
    return tool.permission


def _sanitize_tool_output(text: str) -> str:
    """Strip secrets, forged compaction fences, and `<context>` spans from a
    tool result before persisting it or showing it to the LLM.

    Order matters: scrub spans first (removes whole blocks), then redact
    secret patterns, then defuse any literal FENCE string an attacker might
    have embedded in plain text.
    """
    if not text:
        return text
    out = scrub(text)
    out = redact(out)
    if FENCE in out:
        out = out.replace(FENCE, "[fence-redacted]")
    return out


def _sanitize_tool_args(args: dict[str, Any]) -> dict[str, Any]:
    """Apply secret redaction to argument values before persistence."""
    cleaned: dict[str, Any] = {}
    for k, v in args.items():
        if isinstance(v, str):
            cleaned[k] = redact(v)
        else:
            cleaned[k] = v
    return cleaned


async def _build_view(
    messages: list[dict[str, Any]],
    *,
    llm: LLM,
    target_ratio: float,
) -> list[dict[str, Any]]:
    """Return a compacted view of messages for one LLM call.

    Pure: does not mutate caller state. No-op when under budget.
    """
    budget = getattr(llm, "context_budget", 128_000)
    if estimate_messages(messages) <= int(budget * target_ratio):
        return messages
    summarise = build_summarise_call(llm)
    return await compact_session(
        messages,
        summarise=summarise,
        budget_tokens=budget,
        target_ratio=target_ratio,
    )


async def run_turn(
    session: Session,
    user_msg: str,
    llm: LLM,
    *,
    sink: TokenSink | None = None,
    approval: ApprovalGate | None = None,
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
    system_prompt: str = SYSTEM_PROMPT,
    approval_threshold: Permission = Permission.DANGEROUS,
    compaction_target_ratio: float = 0.8,
    compaction_aggressive_ratio: float = 0.4,
) -> str:
    """Run one full agent turn.

    approval_threshold: tool calls at this tier or above require approval.
    compaction_target_ratio: compact when current tokens exceed ratio * budget.
    compaction_aggressive_ratio: ratio used on retry after a ContextOverflow.
    """
    sink = sink or _stdout_sink()
    approval = approval or AutoApproveGate(allow=False)

    session.append(Message(role="user", content=user_msg))

    if llm.name == "openai":
        tool_schemas = [t.to_openai_schema() for t in all_tools()]
    else:
        tool_schemas = [t.to_anthropic_schema() for t in all_tools()]

    final_text = ""

    for _ in range(max_iterations):
        openai_messages = [m.to_openai() for m in session.messages]
        view = await _build_view(
            openai_messages, llm=llm, target_ratio=compaction_target_ratio,
        )

        assistant_text = ""
        tool_calls: list[dict[str, Any]] = []
        finish_reason = "stop"
        scrubber = StreamingContextScrubber()

        async def _consume(view_messages):
            nonlocal assistant_text, tool_calls, finish_reason
            async for chunk in llm.stream(
                messages=view_messages,
                tools=tool_schemas,
                system=system_prompt,
            ):
                if chunk.delta_text:
                    clean = scrubber.feed(chunk.delta_text)
                    assistant_text += clean
                    sink.on_text(clean)
                if chunk.tool_call_delta:
                    tool_calls.append(chunk.tool_call_delta)
                if chunk.finish_reason:
                    finish_reason = chunk.finish_reason
            tail = scrubber.flush()
            if tail:
                assistant_text += tail
                sink.on_text(tail)

        try:
            await _consume(view)
        except ContextOverflow:
            # Aggressive recompaction + retry once.
            view = await _build_view(
                openai_messages, llm=llm, target_ratio=compaction_aggressive_ratio,
            )
            assistant_text = ""
            tool_calls = []
            finish_reason = "stop"
            scrubber = StreamingContextScrubber()
            await _consume(view)

        # Persist assistant turn (OpenAI tool_calls schema). Arguments stored
        # as JSON; secret values inside arguments are redacted first.
        normalized_tcs = [
            {
                "id": tc["id"],
                "type": "function",
                "function": {
                    "name": tc["name"],
                    "arguments": json.dumps(_sanitize_tool_args(tc["arguments"])),
                },
            }
            for tc in tool_calls
        ]
        # Track the latest assistant text as our running "final answer" — if the
        # loop exits via the max-iterations clause we still surface this rather
        # than discarding it for a hard-coded sentinel.
        if assistant_text:
            final_text = assistant_text
        session.append(
            Message(
                role="assistant",
                content=assistant_text or None,
                tool_calls=normalized_tcs,
            )
        )

        if not tool_calls:
            sink.on_text("\n")
            break

        # Execute each tool call.
        for tc in tool_calls:
            call = ToolCall(
                id=tc["id"],
                name=tc["name"],
                arguments=tc["arguments"],
            )
            sink.on_tool_start(call.name, call.arguments)

            tier = _effective_permission(call)
            if tier >= approval_threshold:
                approved = await approval.request(
                    call, reason=f"{call.name} is {tier.name}"
                )
                if not approved:
                    result_text = "User denied this tool call."
                    sink.on_tool_end(call.name, result_text)
                    session.append(
                        Message(
                            role="tool",
                            content=result_text,
                            tool_call_id=call.id,
                            name=call.name,
                        )
                    )
                    continue

            ctx = ToolContext(session_id=session.id, workspace=session.workspace)
            result = await dispatch_execute(call, ctx)
            sanitised = _sanitize_tool_output(result.content)
            sink.on_tool_end(call.name, sanitised)
            session.append(
                Message(
                    role="tool",
                    content=sanitised,
                    tool_call_id=call.id,
                    name=call.name,
                )
            )

        # Loop back to feed tool results to LLM.
    else:
        if not final_text:
            final_text = "[max iterations reached]"

    return final_text
