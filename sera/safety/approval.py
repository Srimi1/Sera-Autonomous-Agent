"""Approval gate. CLI implementation now; Tauri shell will swap this out at week 6.

Heritage: openhuman approval flow surfaced through UI.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    # Type-only: ToolCall is referenced solely in annotations (PEP 563 strings),
    # so this avoids a runtime safety→tools import — keeping safety a lower layer.
    from sera.safety.vault import EncryptedVault
    from sera.tools.base import ToolCall

log = logging.getLogger("sera.safety.approval")


class ApprovalGate(Protocol):
    async def request(self, call: ToolCall, reason: str = "") -> bool: ...


class CliApprovalGate:
    """Synchronous y/N prompt on stdin via thread executor."""

    async def request(self, call: ToolCall, reason: str = "") -> bool:
        args_str = json.dumps(call.arguments, ensure_ascii=False)
        if len(args_str) > 200:
            args_str = args_str[:200] + f"… [{len(args_str) - 200} chars hidden — review full args before approving]"
        prompt_lines = [
            "",
            "═══ APPROVAL REQUIRED ═══",
            f"  tool: {call.name}",
            f"  args: {args_str}",
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


class VaultApprovalGate:
    """Approval gate backed by the encrypted shape-memory vault (P-64).

    Wraps an inner gate (the actual prompt — CLI or shell). Flow per call:

      1. Fingerprint (tool_name, args) and check the vault.
         - active ALLOW record  → auto-approve, never prompt.
         - active DENY record    → auto-deny (still inside the 24h cooldown).
         - no active record       → fall through to the inner prompt.
      2. After the inner prompt, persist the decision:
         - allow → remembered for `allow_ttl_s` (None = forever, exact shape).
         - deny  → remembered for `deny_cooldown_s` (default 24h cooldown).

    Because the vault is AES-GCM encrypted + authenticated, the remembered
    decisions can't be forged on disk to slip a dangerous call past the gate.
    """

    def __init__(
        self,
        *,
        inner: ApprovalGate,
        vault: "EncryptedVault",
        allow_ttl_s: float | None = None,
        deny_cooldown_s: float = 24 * 3600,
        remember_allow: bool = True,
        remember_deny: bool = True,
    ) -> None:
        self._inner = inner
        self._vault = vault
        self._allow_ttl_s = allow_ttl_s
        self._deny_cooldown_s = deny_cooldown_s
        self._remember_allow = remember_allow
        self._remember_deny = remember_deny

    async def request(self, call: ToolCall, reason: str = "") -> bool:
        existing = self._vault.check_approval(call.name, call.arguments)
        if existing is not None:
            log.debug("vault: auto-%s %s (shape-memory)",
                      "allow" if existing.decision else "deny", call.name)
            return existing.decision

        decision = await self._inner.request(call, reason)

        if decision and self._remember_allow:
            self._vault.remember_approval(
                call.name, call.arguments, decision=True, ttl_s=self._allow_ttl_s,
            )
        elif not decision and self._remember_deny:
            self._vault.remember_approval(
                call.name, call.arguments, decision=False,
                deny_cooldown_s=self._deny_cooldown_s,
            )
        return decision
