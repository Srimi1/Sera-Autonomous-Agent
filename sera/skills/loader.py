"""Skill manifest loader + tool registration."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from sera.tools.base import Permission, Tool, ToolContext, ToolScope

if TYPE_CHECKING:
    # Type-only import: the runtime path keeps `lifecycle` lazily imported inside
    # methods to avoid a circular import (lifecycle ↔ loader).
    from sera.skills.lifecycle import SkillLifecycle

SKILL_TOOL_PREFIX = "skill."
"""Namespace marker so skill-derived tools never collide with system tools."""

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)


class SkillManifestError(ValueError):
    """Malformed or incomplete SKILL.md frontmatter."""


@dataclass(frozen=True)
class Skill:
    name: str
    trigger: str
    permission: str
    version: str
    body: str
    path: Path
    description: str = ""
    args_schema: dict[str, Any] | None = None
    lineage: tuple[str, ...] = ()
    council: bool = False


REQUIRED_FIELDS = ("name", "trigger", "permission", "version")


def load_skill(path: Path) -> Skill:
    text = Path(path).read_text()
    m = _FRONTMATTER_RE.match(text)
    if not m:
        raise SkillManifestError(f"{path}: missing YAML frontmatter")
    meta = yaml.safe_load(m.group(1)) or {}
    if not isinstance(meta, dict):
        raise SkillManifestError(f"{path}: frontmatter is not a mapping")
    missing = [f for f in REQUIRED_FIELDS if not meta.get(f)]
    if missing:
        raise SkillManifestError(
            f"{path}: missing required field(s): {', '.join(missing)}"
        )
    body = text[m.end():].strip()

    raw_lineage = meta.get("lineage")
    if raw_lineage is None:
        lineage: tuple[str, ...] = ()
    elif isinstance(raw_lineage, str):
        lineage = (raw_lineage,)
    elif isinstance(raw_lineage, (list, tuple)):
        lineage = tuple(str(x) for x in raw_lineage)
    else:
        raise SkillManifestError(
            f"{path}: `lineage` must be a string or list of strings"
        )

    raw_schema = meta.get("args_schema")
    if raw_schema is not None and not isinstance(raw_schema, dict):
        raise SkillManifestError(f"{path}: `args_schema` must be a mapping")

    return Skill(
        name=meta["name"],
        trigger=meta["trigger"],
        permission=meta["permission"],
        version=meta["version"],
        body=body,
        path=Path(path),
        description=str(meta.get("description") or ""),
        args_schema=raw_schema,
        lineage=lineage,
        council=bool(meta.get("council", False)),
    )


def skill_to_tool(skill: Skill) -> Tool:
    """Adapt a `Skill` manifest into a callable `Tool` registry entry.

    The tool name is namespaced under `skill.` so it can't collide with
    a system tool of the same short name. The handler returns the skill's
    body — skeleton semantics. P-22+ can swap the handler for a real
    executor (LLM-driven skill invocation) without changing this contract.
    """
    permission = Permission.parse(skill.permission)
    parameters = skill.args_schema or {"type": "object", "properties": {}}

    async def _handler(args: dict[str, Any], ctx: ToolContext) -> str:
        return skill.body

    return Tool(
        name=f"{SKILL_TOOL_PREFIX}{skill.name}",
        description=skill.description or skill.body.split("\n\n", 1)[0],
        parameters=parameters,
        permission=permission,
        scope=ToolScope.SKILL,
        handler=_handler,
    )


def discover_skills(root: Path) -> list[Skill]:
    """Load every `<root>/<name>/SKILL.md` and return them sorted by name."""
    root = Path(root)
    if not root.is_dir():
        return []
    out: list[Skill] = []
    for child in sorted(root.iterdir()):
        manifest = child / "SKILL.md"
        if child.is_dir() and manifest.is_file():
            out.append(load_skill(manifest))
    return out


# ─── Hot-reloadable registry ───────────────────────────────────


class SkillRegistry:
    """Owns the mapping skill_name → registered Tool name.

    `refresh()` rescans the skills root, registers new manifests, drops
    skills that disappeared from disk, and re-registers ones whose
    SKILL.md mtime changed. The global `sera.tools.registry` is the
    write-through target — skill-derived tools appear via `sera tools`
    without any further plumbing.

    When a `lifecycle` is supplied, the registry consults `state_of` on
    every refresh: ARCHIVED skills are skipped (and unregistered if
    they had been live), every successful registration also `touch()`es
    the lifecycle row so freshness-aware sweeps see the access.
    """

    def __init__(self, root: Path, *, lifecycle: "SkillLifecycle | None" = None) -> None:
        self.root = Path(root)
        self.lifecycle = lifecycle  # lazy runtime import avoids the circular dep
        # name → (tool_name, manifest_mtime)
        self._tracked: dict[str, tuple[str, float]] = {}

    def _is_archived(self, name: str) -> bool:
        if self.lifecycle is None:
            return False
        # Lazy import to avoid the lifecycle module pulling skills.loader at
        # import time (circular).
        from sera.skills.lifecycle import LifecycleState

        return self.lifecycle.state_of(name) is LifecycleState.ARCHIVED

    def _is_runtime_eligible(self, name: str) -> bool:
        """Combined gate: not archived AND (verified OR pinned).

        Pinned skills bypass the verification gate — pin is the user's
        explicit override (e.g. inherited trusted skill, internal tools).
        """
        if self.lifecycle is None:
            return True
        if self._is_archived(name):
            return False
        if self.lifecycle.is_verified(name):
            return True
        row = self.lifecycle.get(name)
        return bool(row and row.pinned)

    def refresh(self) -> "RefreshSummary":
        """Re-scan disk; sync the tool registry. Idempotent across runs."""
        from sera.tools import registry as tool_registry

        added: list[str] = []
        removed: list[str] = []
        updated: list[str] = []

        seen_live: set[str] = set()
        for skill in discover_skills(self.root):
            if not self._is_runtime_eligible(skill.name):
                # Archived or unverified candidate — treat as deleted from
                # the runtime view. Cleanup pass below unregisters if the
                # tool was live in a previous refresh.
                continue
            seen_live.add(skill.name)
            manifest_mtime = skill.path.stat().st_mtime
            prior = self._tracked.get(skill.name)
            if prior is None:
                tool = skill_to_tool(skill)
                tool_registry.register(tool)
                self._tracked[skill.name] = (tool.name, manifest_mtime)
                added.append(skill.name)
                if self.lifecycle is not None:
                    self.lifecycle.touch(skill.name)
                continue
            tool_name, last_mtime = prior
            if manifest_mtime != last_mtime:
                tool = skill_to_tool(skill)
                tool_registry.register(tool)
                self._tracked[skill.name] = (tool.name, manifest_mtime)
                updated.append(skill.name)
                if self.lifecycle is not None:
                    self.lifecycle.touch(skill.name)

        for name in list(self._tracked):
            if name in seen_live:
                continue
            tool_name, _ = self._tracked.pop(name)
            tool_registry.unregister(tool_name)
            removed.append(name)

        return RefreshSummary(
            added=tuple(added), removed=tuple(removed), updated=tuple(updated)
        )

    def tools(self) -> list[Tool]:
        """Return the currently-registered skill tools (snapshot)."""
        from sera.tools import registry as tool_registry

        out: list[Tool] = []
        for _name, (tool_name, _mtime) in self._tracked.items():
            t = tool_registry.get(tool_name)
            if t is not None:
                out.append(t)
        return out

    def clear(self) -> None:
        """Unregister every skill this instance owns. Useful in tests."""
        from sera.tools import registry as tool_registry

        for _name, (tool_name, _mtime) in list(self._tracked.items()):
            tool_registry.unregister(tool_name)
        self._tracked.clear()


@dataclass(frozen=True)
class RefreshSummary:
    """Delta from one `SkillRegistry.refresh()` call."""

    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    updated: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        return bool(self.added or self.removed or self.updated)


# ─── Default per-root singleton ────────────────────────────────


_DEFAULT_REGISTRIES: dict[str, SkillRegistry] = {}


def get_default_registry(
    root: Path,
    *,
    lifecycle: "SkillLifecycle | None" = None,
) -> SkillRegistry:
    """Process-wide singleton SkillRegistry keyed by root path.

    Lets multiple CLI invocations (or repeated agent turns) reuse one
    registry and see real deltas — second `refresh()` against an
    unchanged directory reports zero changes, exactly the hot-reload
    contract the phase wants.
    """
    key = str(Path(root).resolve())
    reg = _DEFAULT_REGISTRIES.get(key)
    if reg is None:
        reg = SkillRegistry(root=Path(root), lifecycle=lifecycle)
        _DEFAULT_REGISTRIES[key] = reg
    elif lifecycle is not None and reg.lifecycle is None:
        reg.lifecycle = lifecycle
    return reg


def reset_default_registries() -> None:
    """Test helper — drops every cached singleton and clears their tools."""
    for reg in list(_DEFAULT_REGISTRIES.values()):
        reg.clear()
    _DEFAULT_REGISTRIES.clear()
