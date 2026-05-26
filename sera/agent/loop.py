"""ReAct agent loop. Heritage: hermes/agent/conversation_loop.py:526."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Callable

from sera.agent.budget import IterationBudget, MaxIterations
from sera.llm.budget import BudgetEnforcer, BudgetExceeded
from sera.llm.distill_cache import DistillCache, compute_key as _distill_key
from sera.agent.interrupt import InterruptToken
from sera.context.compressor import FENCE, build_summarise_call, compact_session
from sera.context.scrubber import StreamingContextScrubber, scrub
from sera.context.tokenjuice import DEFAULT_COMPRESS_THRESHOLD, compress_sync
from sera.context.tokens import estimate_messages
from sera.llm.base import LLM, ContextOverflow
from sera.llm.cache import freeze_system_prompt
from sera.memory.session import Message, Session
from sera.profile import build_profile_prompt
from sera.safety.approval import ApprovalGate, AutoApproveGate
from sera.safety.redact import redact
from sera.skills.manifest import CouncilConfig, council_skill_dispatch
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

GRACE_NOTICE = (
    "[iteration budget exhausted — produce your final answer now in plain text. "
    "Do not call any tools.]"
)


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
    secret patterns, defuse any literal FENCE, then — for outputs above the
    TokenJuice threshold — run the rule-based compressor. Compression runs
    last so HTML/URL/table rewrites can't reintroduce a scrubbed span.
    """
    if not text:
        return text
    out = scrub(text)
    out = redact(out)
    if FENCE in out:
        out = out.replace(FENCE, "[fence-redacted]")
    if len(out) >= DEFAULT_COMPRESS_THRESHOLD:
        out = compress_sync(out).text
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


def _council_question(call: ToolCall) -> str:
    """Extract a natural-language question from a skill tool call's arguments."""
    args = call.arguments or {}
    for key in ("query", "question", "input", "prompt", "text"):
        if key in args and isinstance(args[key], str):
            return args[key]
    texts = [v for v in args.values() if isinstance(v, str)]
    return " ".join(texts) if texts else call.name


async def run_turn(
    session: Session,
    user_msg: str,
    llm: LLM,
    *,
    sink: TokenSink | None = None,
    approval: ApprovalGate | None = None,
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
    budget: IterationBudget | None = None,
    interrupt: InterruptToken | None = None,
    system_prompt: str = SYSTEM_PROMPT,
    approval_threshold: Permission = Permission.DANGEROUS,
    compaction_target_ratio: float = 0.8,
    compaction_aggressive_ratio: float = 0.4,
    council_config: CouncilConfig | None = None,
    cost_enforcer: BudgetEnforcer | None = None,
    distill_cache: DistillCache | None = None,
    workshop: object | None = None,
    profile_learner: object | None = None,
) -> str:
    """Run one full agent turn.

    approval_threshold: tool calls at this tier or above require approval.
    compaction_target_ratio: compact when current tokens exceed ratio * budget.
    compaction_aggressive_ratio: ratio used on retry after a ContextOverflow.
    budget: shared IterationBudget across parent + future subagents. If None,
        a fresh one is built from `max_iterations`.
    interrupt: per-turn cancel token. If None, a fresh one is allocated (and
        nothing will ever set it from outside).
    """
    sink = sink or _stdout_sink()
    approval = approval or AutoApproveGate(allow=False)
    budget = budget or IterationBudget.of(max_iterations)
    interrupt = interrupt or InterruptToken()

    # Freeze the system prompt on first turn; on resume, restore the frozen
    # prompt verbatim so Anthropic's prompt cache keeps hitting.
    profile_prompt = build_profile_prompt(session.workspace)
    combined_prompt = system_prompt
    if profile_prompt:
        combined_prompt = f"{system_prompt}\n\n{profile_prompt}"
    frozen_prompt = freeze_system_prompt(session, combined_prompt)

    session.append(Message(role="user", content=user_msg))

    if llm.name == "openai":
        tool_schemas = [t.to_openai_schema() for t in all_tools()]
    else:
        tool_schemas = [t.to_anthropic_schema() for t in all_tools()]

    # Distillation cache: check before entering the LLM loop.
    _distill_key_val: str | None = None
    _distill_hit = False
    if distill_cache is not None:
        try:
            _tool_msgs = [m.to_openai() for m in session.messages if m.role == "tool"]
            _distill_key_val = _distill_key(user_msg, _tool_msgs)
            _cached = distill_cache.get(_distill_key_val)
            if _cached is not None:
                sink.on_text(_cached)
                sink.on_text("\n")
                _distill_hit = True
                session.last_turn_cost_usd = 0.0  # cache hit — no LLM call
                return _cached
        except Exception:
            pass

    final_text = ""
    grace_mode = False
    _turn_cost: float = 0.0

    while True:
        interrupt.check()
        try:
            budget.consume()
        except MaxIterations:
            if budget.can_request_grace():
                budget.request_grace()
                budget.consume()
                grace_mode = True
                session.append(Message(role="user", content=GRACE_NOTICE))
            else:
                if not final_text:
                    final_text = "[max iterations reached]"
                break

        if cost_enforcer is not None:
            _budget_check = cost_enforcer.check()
            if _budget_check.blocked:
                raise BudgetExceeded(_budget_check.message)

        openai_messages = [m.to_openai() for m in session.messages]
        view = await _build_view(
            openai_messages, llm=llm, target_ratio=compaction_target_ratio,
        )

        assistant_text = ""
        tool_calls: list[dict[str, Any]] = []
        finish_reason = "stop"
        usage: dict[str, int] | None = None
        scrubber = StreamingContextScrubber()

        active_tools = None if grace_mode else tool_schemas

        async def _consume(view_messages):
            nonlocal assistant_text, finish_reason, usage
            async for chunk in llm.stream(
                messages=view_messages,
                tools=active_tools,
                system=frozen_prompt,
            ):
                if chunk.delta_text:
                    clean = scrubber.feed(chunk.delta_text)
                    assistant_text += clean
                    sink.on_text(clean)
                if chunk.tool_call_delta:
                    tool_calls.append(chunk.tool_call_delta)
                if chunk.finish_reason:
                    finish_reason = chunk.finish_reason
                if chunk.usage:
                    usage = chunk.usage
            tail = scrubber.flush()
            if tail:
                assistant_text += tail
                sink.on_text(tail)

        _t0 = time.monotonic()
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
            usage = None
            scrubber = StreamingContextScrubber()
            await _consume(view)
        _latency_ms = int((time.monotonic() - _t0) * 1000)

        if usage:
            session.record_usage(
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
                cache_read_tokens=usage.get("cache_read_input_tokens", 0),
                cache_creation_tokens=usage.get("cache_creation_input_tokens", 0),
            )

        try:
            from sera.llm.router_stats import _calc_cost, record_call as _record_call
            _task_kind = "tool" if tool_calls else "chat"
            _in_tok = usage.get("input_tokens", 0) if usage else 0
            _out_tok = usage.get("output_tokens", 0) if usage else 0
            _call_cost = _calc_cost(getattr(llm, "model", ""), _in_tok, _out_tok)
            _record_call(
                provider=llm.name,
                model=getattr(llm, "model", "unknown"),
                task_kind=_task_kind,
                latency_ms=_latency_ms,
                input_tokens=_in_tok,
                output_tokens=_out_tok,
                success=True,
            )
            if cost_enforcer is not None:
                cost_enforcer.add(_call_cost, task_kind=_task_kind)
            _turn_cost += _call_cost
        except BudgetExceeded:
            raise
        except Exception:
            pass

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
                finish_reason=finish_reason,
            )
        )

        if not tool_calls:
            sink.on_text("\n")
            break

        if grace_mode:
            # Grace turn must not request tools; if the model still emitted
            # any (rare), drop them and treat the assistant text as final.
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
            if council_config and call.name in council_config.council_skills:
                question = _council_question(call)
                synthesis = await council_skill_dispatch(question, council_config)
                sanitised = _sanitize_tool_output(synthesis)
            else:
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
            interrupt.check()

    # Store successful response in distillation cache for future hits.
    if distill_cache is not None and _distill_key_val and final_text and not _distill_hit:
        try:
            distill_cache.put(_distill_key_val, final_text, cost_usd=_turn_cost)
        except Exception:
            pass

    if workshop is not None:
        capture = getattr(workshop, "capture_session", None)
        if capture is not None:
            try:
                await capture(session)
            except Exception:
                pass

    if profile_learner is not None:
        capture = getattr(profile_learner, "capture_session", None)
        if capture is not None:
            try:
                await capture(session)
            except Exception:
                pass

    session.last_turn_cost_usd = _turn_cost
    return final_text
