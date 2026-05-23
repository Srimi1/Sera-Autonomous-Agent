"""python_eval — run Python code in a tiered sandbox.

Tiered sandboxes (P-44 outclass):
  LOCAL    $0.00  — subprocess + timeout + network AST guard
  MODAL    ~$0.01 — Modal cloud function (if installed + configured)
  DAYTONA  ~$0.10 — Daytona dev environment (if token configured)

The picker selects the cheapest tier within the configured cost ceiling.
Infinite loops are killed at 10s. Network imports require explicit grant.
"""
from __future__ import annotations

from typing import Any

from sera.sandbox.picker import pick_sandbox
from sera.tools.base import Permission, Tool, ToolContext, ToolScope
from sera.tools.registry import register


async def _handler(args: dict[str, Any], ctx: ToolContext) -> str:
    code = args.get("code", "").strip()
    if not code:
        return "[python_eval: code is required]"

    timeout = float(args.get("timeout", 10.0))
    allow_network = bool(args.get("allow_network", False))
    cost_ceiling = float(args.get("cost_ceiling_usd", 0.0))

    sandbox = pick_sandbox(
        cost_ceiling_usd=cost_ceiling,
        require_network=allow_network,
    )

    result = await sandbox.run(code, timeout=timeout, allow_network=allow_network)
    return result.as_tool_output()


register(Tool(
    name="python_eval",
    description=(
        "Execute Python code in a sandboxed subprocess. "
        "Killed after timeout_s (default 10). "
        "Network imports (requests, socket, etc.) are blocked unless allow_network=true. "
        "Set cost_ceiling_usd > 0.01 to escalate to Modal cloud; > 0.10 for Daytona."
    ),
    parameters={
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "Python source code to execute.",
            },
            "timeout": {
                "type": "number",
                "description": "Max execution time in seconds. Default 10.",
                "default": 10,
            },
            "allow_network": {
                "type": "boolean",
                "description": "Allow network imports (requests, socket, etc.). Default false.",
                "default": False,
            },
            "cost_ceiling_usd": {
                "type": "number",
                "description": "Max acceptable sandbox cost per run. 0=local only.",
                "default": 0.0,
            },
        },
        "required": ["code"],
    },
    permission=Permission.EXECUTE,
    scope=ToolScope.SYSTEM,
    handler=_handler,
))
