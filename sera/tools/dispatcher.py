"""Tool dispatcher — executes a ToolCall via the registry.

Heritage: openhuman/agent/dispatcher.rs (trait). Sync handlers run in a
worker thread via ``asyncio.to_thread`` so blocking IO doesn't pin the loop.
Errors surface as ``ToolResult(error=True)`` with a redacted short message;
full tracebacks are logged locally and never returned to the LLM.
"""
from __future__ import annotations

import asyncio
import logging
import traceback

from sera.safety.redact import redact
from sera.tools.base import ToolCall, ToolContext, ToolResult
from sera.tools.registry import get

log = logging.getLogger("sera.tools.dispatcher")


async def execute(call: ToolCall, ctx: ToolContext) -> ToolResult:
    tool = get(call.name)
    if tool is None:
        return ToolResult(call.id, call.name, f"Unknown tool: {call.name}", error=True)
    try:
        if asyncio.iscoroutinefunction(tool.handler):
            content = await tool.handler(call.arguments, ctx)
        else:
            content = await asyncio.to_thread(tool.handler, call.arguments, ctx)
        return ToolResult(call.id, call.name, str(content))
    except Exception as e:  # noqa: BLE001 — surface tool errors as results
        log.warning("tool %s failed", call.name, exc_info=True)
        log.debug("full traceback:\n%s", traceback.format_exc())
        short = redact(f"{type(e).__name__}: {e}")
        return ToolResult(call.id, call.name, short, error=True)
