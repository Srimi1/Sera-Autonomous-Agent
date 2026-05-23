"""Subagent delegation — parent spawns a subagent with a shared iteration budget.

Outclass (built on P-07): shared IterationBudget means subagent iterations count
against the parent's pool. Hermes tracks iterations per-agent, so a parent at its
limit can still spin up an unbounded subagent. Sera cannot — the budget is one pool.

Usage:
    # As a standalone async call:
    result = await delegate_task("summarize this PDF", llm, budget=shared_budget)

    # As a registered tool (called by the agent via tool_calls):
    tool = make_delegate_tool(llm, budget)
    register(tool)
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from sera.agent.budget import IterationBudget
from sera.agent.loop import SYSTEM_PROMPT, TokenSink, run_turn
from sera.llm.base import LLM
from sera.memory.session import Session
from sera.tools.base import Permission, Tool, ToolContext, ToolScope
from sera.tools.registry import register


# ---------------------------------------------------------------------------
# Core delegation primitive
# ---------------------------------------------------------------------------

async def delegate_task(
    prompt: str,
    llm: LLM,
    *,
    budget: IterationBudget,
    workspace: str | None = None,
    system_prompt: str = SYSTEM_PROMPT,
    context: str = "",
) -> str:
    """Run a subtask in an isolated session sharing the caller's IterationBudget.

    The subagent has a fresh conversation history and no access to the parent's
    context. Iterations consumed here decrement the shared budget — preventing
    runaway recursive delegation.

    Args:
        prompt:        Task description for the subagent.
        llm:           LLM adapter to use (same as or different from parent).
        budget:        Shared IterationBudget — consumed by both parent and subagent.
        workspace:     Workspace root for tool calls (defaults to a temp dir).
        system_prompt: System prompt override for the subagent.
        context:       Optional background context prepended to the prompt.

    Returns:
        The subagent's final response text.
    """
    full_prompt = f"{context.strip()}\n\n{prompt}".strip() if context else prompt

    with tempfile.TemporaryDirectory(prefix="sera_delegate_") as tmpdir:
        db_path = Path(tmpdir) / "session.db"
        ws = workspace or tmpdir

        session = Session.create(workspace=ws, db_path=db_path)

        # Capture subagent output in a buffer — don't write to stdout
        _buf: list[str] = []
        sink = TokenSink(on_text=lambda t: _buf.append(t))

        result = await run_turn(
            session,
            full_prompt,
            llm,
            sink=sink,
            budget=budget,
            system_prompt=system_prompt,
        )

    return result


# ---------------------------------------------------------------------------
# Tool factory — makes delegate_task callable by the agent via tool_call
# ---------------------------------------------------------------------------

def make_delegate_tool(llm: LLM, budget: IterationBudget) -> Tool:
    """Build a Tool that lets the agent spawn a subagent via tool call.

    The returned Tool captures `llm` and `budget` in a closure. Register it
    before starting the agent turn to expose it in the tool list.

    Example:
        tool = make_delegate_tool(llm, budget)
        register(tool)
        await run_turn(session, user_msg, llm, budget=budget)
    """

    async def _handler(args: dict[str, Any], ctx: ToolContext) -> str:
        prompt = args.get("prompt", "").strip()
        if not prompt:
            return "[delegate_task: prompt is required]"
        context = args.get("context", "")
        return await delegate_task(
            prompt,
            llm,
            budget=budget,
            workspace=ctx.workspace,
            context=context,
        )

    return Tool(
        name="delegate_task",
        description=(
            "Delegate a subtask to a subagent running in an isolated session. "
            "The subagent shares this turn's iteration budget — use sparingly. "
            "Returns the subagent's complete response as a string."
        ),
        parameters={
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "The task to delegate. Be specific — the subagent has no parent context.",
                },
                "context": {
                    "type": "string",
                    "description": "Optional background context to include before the prompt.",
                },
            },
            "required": ["prompt"],
        },
        permission=Permission.EXECUTE,
        scope=ToolScope.SYSTEM,
        handler=_handler,
    )
