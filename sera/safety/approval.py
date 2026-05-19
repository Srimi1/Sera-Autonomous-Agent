"""Approval gate. CLI implementation now; Tauri shell will swap this out at week 6.

Heritage: openhuman approval flow surfaced through UI.
"""
from __future__ import annotations

import asyncio
import json
from typing import Protocol

from sera.tools.base import ToolCall


class ApprovalGate(Protocol):
    async def request(self, call: ToolCall, reason: str = "") -> bool: ...


class CliApprovalGate:
    """Synchronous y/N prompt on stdin via thread executor."""

    async def request(self, call: ToolCall, reason: str = "") -> bool:
        prompt_lines = [
            "",
            "═══ APPROVAL REQUIRED ═══",
            f"  tool: {call.name}",
            f"  args: {json.dumps(call.arguments, ensure_ascii=False)[:200]}",
        ]
        if reason:
            prompt_lines.append(f"  reason: {reason}")
        prompt_lines.append("Allow this tool call? [y/N]: ")
        prompt = "\n".join(prompt_lines)

        answer = await asyncio.to_thread(input, prompt)
        return answer.strip().lower() in ("y", "yes")


class AutoApproveGate:
    """For tests + non-interactive runs. Always denies dangerous tools."""

    def __init__(self, allow: bool = False) -> None:
        self.allow = allow

    async def request(self, call: ToolCall, reason: str = "") -> bool:
        return self.allow
