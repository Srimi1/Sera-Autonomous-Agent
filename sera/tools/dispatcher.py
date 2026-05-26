"""Tool dispatcher — executes a ToolCall via the registry.

Heritage: openhuman/agent/dispatcher.rs (trait). Sync handlers run in a
worker thread via ``asyncio.to_thread`` so blocking IO doesn't pin the loop.
Errors surface as ``ToolResult(error=True)`` with a redacted short message;
full tracebacks are logged locally and never returned to the LLM.

Per-tool stats (P-50) are recorded after every call — usage, latency, success,
errors — visible via `sera tools --stats`.
"""
from __future__ import annotations

import asyncio
import logging
import time
import traceback
from typing import Any, Callable, cast

from sera.safety.redact import redact
from sera.tools.base import ToolCall, ToolContext, ToolResult
from sera.tools.registry import get

log = logging.getLogger("sera.tools.dispatcher")


async def execute(call: ToolCall, ctx: ToolContext) -> ToolResult:
    tool = get(call.name)
    if tool is None:
        return ToolResult(call.id, call.name, f"Unknown tool: {call.name}", error=True)
    t0 = time.monotonic()
    success = True
    error_msg: str | None = None
    try:
        if asyncio.iscoroutinefunction(tool.handler):
            content = await tool.handler(call.arguments, ctx)
        else:
            # Reached only for sync handlers; ToolHandler is typed async, so cast.
            sync_handler = cast(Callable[[dict[str, Any], ToolContext], str], tool.handler)
            content = await asyncio.to_thread(sync_handler, call.arguments, ctx)
        result = ToolResult(call.id, call.name, str(content))
    except Exception as e:  # noqa: BLE001 — surface tool errors as results
        log.warning("tool %s failed", call.name, exc_info=True)
        log.debug("full traceback:\n%s", traceback.format_exc())
        short = redact(f"{type(e).__name__}: {e}")
        success = False
        error_msg = short
        result = ToolResult(call.id, call.name, short, error=True)
    finally:
        latency_ms = int((time.monotonic() - t0) * 1000)
        try:
            from sera.tools.stats import record_tool_call
            record_tool_call(
                tool_name=call.name,
                latency_ms=latency_ms,
                success=success,
                error_msg=error_msg,
            )
        except Exception:
            pass
    return result
