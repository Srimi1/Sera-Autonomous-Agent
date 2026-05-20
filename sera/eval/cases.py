"""Eval case schema + YAML loader.

A case bundles:

  * a user prompt
  * an optional `script` of stubbed LLM responses (text/tool_calls per step)
  * an `expect` block — substrings, required tool calls, forbidden tools,
    iteration bounds
  * optional `workspace_files` — files seeded into the per-case temp workspace
    before run_turn fires (lets file_read cases test against real disk)

Cases run in isolated temp workspaces and isolated SQLite DBs. The harness
never touches the user's real sessions.db.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class ScriptStep:
    """One LLM turn in a stubbed run.

    `text` and `tool_calls` are both optional but at least one must be set.
    `finish_reason` defaults to "stop" if no tool_calls, else "tool_calls".
    """

    text: str = ""
    tool_calls: tuple[dict[str, Any], ...] = ()
    finish_reason: str | None = None

    @property
    def effective_finish_reason(self) -> str:
        if self.finish_reason:
            return self.finish_reason
        return "tool_calls" if self.tool_calls else "stop"


@dataclass(frozen=True)
class ExpectedOutcome:
    """Pass criteria for a case. Empty fields are skipped."""

    substring: str = ""
    tool_calls: tuple[str, ...] = ()
    forbid_tool_calls: tuple[str, ...] = ()
    min_iterations: int = 0
    max_iterations: int = 0  # 0 = unbounded


@dataclass(frozen=True)
class EvalCase:
    id: str
    prompt: str
    script: tuple[ScriptStep, ...] = ()
    expect: ExpectedOutcome = field(default_factory=ExpectedOutcome)
    workspace_files: dict[str, str] = field(default_factory=dict)
    tags: tuple[str, ...] = ()


def _coerce_steps(raw: list[dict[str, Any]] | None) -> tuple[ScriptStep, ...]:
    if not raw:
        return ()
    out: list[ScriptStep] = []
    for s in raw:
        tcs_raw = s.get("tool_calls") or []
        tcs = tuple(
            {
                "id": tc.get("id") or f"tc{i}",
                "name": tc["name"],
                "arguments": tc.get("arguments") or {},
            }
            for i, tc in enumerate(tcs_raw)
        )
        out.append(
            ScriptStep(
                text=s.get("text", "") or "",
                tool_calls=tcs,
                finish_reason=s.get("finish_reason"),
            )
        )
    return tuple(out)


def _coerce_expect(raw: dict[str, Any] | None) -> ExpectedOutcome:
    raw = raw or {}
    return ExpectedOutcome(
        substring=raw.get("substring", "") or "",
        tool_calls=tuple(raw.get("tool_calls") or ()),
        forbid_tool_calls=tuple(raw.get("forbid_tool_calls") or ()),
        min_iterations=int(raw.get("min_iterations") or 0),
        max_iterations=int(raw.get("max_iterations") or 0),
    )


def load_case(path: Path) -> EvalCase:
    """Parse one yaml case file."""
    with path.open("r") as f:
        raw = yaml.safe_load(f) or {}
    return EvalCase(
        id=raw.get("id") or path.stem,
        prompt=raw["prompt"],
        script=_coerce_steps(raw.get("script")),
        expect=_coerce_expect(raw.get("expect")),
        workspace_files=raw.get("workspace_files") or {},
        tags=tuple(raw.get("tags") or ()),
    )


def load_cases(directory: Path) -> list[EvalCase]:
    """Load every `*.yaml` in `directory`, sorted by filename for stable runs."""
    files = sorted(directory.glob("*.yaml"))
    return [load_case(f) for f in files]
