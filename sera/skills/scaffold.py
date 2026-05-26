"""Structured skill package scaffolding."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class SkillScaffoldResult:
    skill_dir: Path
    skill_path: Path
    replay_path: Path


def slugify_skill_name(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    if not slug:
        raise ValueError("skill name must contain letters or digits")
    return slug


def scaffold_skill(
    root: Path,
    *,
    name: str,
    trigger: str | None = None,
    description: str = "",
    permission: str = "READ_ONLY",
    version: str = "0.1.0",
    body: str = "",
    replay_substring: str = "",
    force: bool = False,
) -> SkillScaffoldResult:
    slug = slugify_skill_name(name)
    skill_dir = Path(root) / slug
    skill_path = skill_dir / "SKILL.md"
    replay_path = skill_dir / "replay.yaml"
    if skill_dir.exists() and not force:
        raise FileExistsError(f"{skill_dir} already exists")
    skill_dir.mkdir(parents=True, exist_ok=True)

    default_body = (
        f"# {slug.replace('-', ' ').title()}\n\n"
        "Use this workflow when the same task recurs.\n\n"
        "- Follow the documented steps in order.\n"
        "- Verify the result before responding.\n"
    )
    chosen_body = (body.strip() or default_body).rstrip() + "\n"
    meta = {
        "name": slug,
        "description": description or f"Reusable workflow for {slug.replace('-', ' ')}.",
        "trigger": trigger or f"/{slug}",
        "permission": permission,
        "version": version,
    }
    skill_text = f"---\n{yaml.safe_dump(meta, sort_keys=False).strip()}\n---\n\n{chosen_body}"
    skill_path.write_text(skill_text)

    expect = replay_substring.strip() or _default_replay_expectation(chosen_body)
    replay = {
        "skill": slug,
        "cases": [
            {
                "id": "smoke",
                "input": {},
                "expect": {"substring": expect},
            }
        ],
    }
    replay_path.write_text(yaml.safe_dump(replay, sort_keys=False))
    return SkillScaffoldResult(
        skill_dir=skill_dir,
        skill_path=skill_path,
        replay_path=replay_path,
    )


def _default_replay_expectation(body: str) -> str:
    for line in body.splitlines():
        clean = line.strip().lstrip("-").strip()
        if clean and not clean.startswith("#"):
            return clean[:80]
    return "Use this workflow"
