"""Sandbox base types — tier enum, result, protocol."""
from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


class SandboxTier(enum.Enum):
    LOCAL = "local"      # subprocess on this machine, free
    MODAL = "modal"      # Modal cloud function, ~$0.01/run
    DAYTONA = "daytona"  # Daytona dev environment, higher cost


@dataclass
class SandboxResult:
    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool
    tier: SandboxTier

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    def as_tool_output(self) -> str:
        """Format for LLM consumption."""
        parts: list[str] = []
        if self.timed_out:
            parts.append("[sandbox: execution timed out]")
        if self.stdout.strip():
            parts.append(self.stdout.rstrip())
        if self.stderr.strip():
            parts.append(f"[stderr]\n{self.stderr.rstrip()}")
        if not parts:
            if self.exit_code != 0:
                return f"[exit {self.exit_code}]"
            return "[no output]"
        if self.exit_code != 0 and not self.timed_out:
            parts.append(f"[exit {self.exit_code}]")
        return "\n".join(parts)


@runtime_checkable
class Sandbox(Protocol):
    tier: SandboxTier

    async def run(
        self,
        code: str,
        *,
        timeout: float = 10.0,
        allow_network: bool = False,
    ) -> SandboxResult: ...
