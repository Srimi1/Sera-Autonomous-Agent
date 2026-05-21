"""Skill manifest loader."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

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
        args_schema=raw_schema,
        lineage=lineage,
        council=bool(meta.get("council", False)),
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
