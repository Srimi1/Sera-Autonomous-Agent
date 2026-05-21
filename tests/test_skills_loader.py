"""P-21: skills loader + manifest (TDD)."""
from __future__ import annotations

from pathlib import Path

import pytest

from sera.skills.loader import (
    Skill,
    SkillManifestError,
    discover_skills,
    load_skill,
)


def _write_skill(dir_: Path, body: str) -> Path:
    dir_.mkdir(parents=True, exist_ok=True)
    p = dir_ / "SKILL.md"
    p.write_text(body)
    return p


_MIN_FRONTMATTER = {
    "name": "x",
    "trigger": "/x",
    "permission": "READ",
    "version": "0.1.0",
}


def _frontmatter(meta: dict, body: str = "body") -> str:
    import yaml as _yaml
    return f"---\n{_yaml.safe_dump(meta).strip()}\n---\n\n{body}\n"


# ─── Cycle 1: tracer bullet ─────────────────────────────────────


def test_load_minimal_skill_returns_required_fields(tmp_path: Path):
    body = """---
name: caveman
trigger: /caveman
permission: READ
version: 0.1.0
---
Caveman mode — compressed responses.
"""
    p = _write_skill(tmp_path / "caveman", body)
    skill = load_skill(p)
    assert isinstance(skill, Skill)
    assert skill.name == "caveman"
    assert skill.trigger == "/caveman"
    assert skill.permission == "READ"
    assert skill.version == "0.1.0"
    assert skill.body.startswith("Caveman mode")


# ─── Cycle 2: missing required field raises ────────────────────


@pytest.mark.parametrize("missing", ["name", "trigger", "permission", "version"])
def test_missing_required_field_raises(tmp_path: Path, missing):
    meta = dict(_MIN_FRONTMATTER)
    del meta[missing]
    p = _write_skill(tmp_path / "broken", _frontmatter(meta))
    with pytest.raises(SkillManifestError) as exc:
        load_skill(p)
    assert missing in str(exc.value)


def test_no_frontmatter_raises(tmp_path: Path):
    p = _write_skill(tmp_path / "nofm", "no frontmatter here\n")
    with pytest.raises(SkillManifestError):
        load_skill(p)


# ─── Cycle 3: optional fields ──────────────────────────────────


def test_optional_fields_default_when_absent(tmp_path: Path):
    p = _write_skill(tmp_path / "min", _frontmatter(_MIN_FRONTMATTER))
    skill = load_skill(p)
    assert skill.args_schema is None
    assert skill.lineage == ()
    assert skill.council is False


def test_optional_fields_populate_when_present(tmp_path: Path):
    meta = {
        **_MIN_FRONTMATTER,
        "args_schema": {"type": "object", "properties": {"x": {"type": "string"}}},
        "lineage": ["hermes/skills/foo", "openhuman/skills/bar"],
        "council": True,
    }
    p = _write_skill(tmp_path / "rich", _frontmatter(meta))
    skill = load_skill(p)
    assert skill.args_schema == meta["args_schema"]
    assert skill.lineage == ("hermes/skills/foo", "openhuman/skills/bar")
    assert skill.council is True


def test_lineage_string_coerces_to_tuple(tmp_path: Path):
    """Single-string lineage in yaml shouldn't crash — wrap it."""
    meta = {**_MIN_FRONTMATTER, "lineage": "hermes/single-source"}
    p = _write_skill(tmp_path / "one", _frontmatter(meta))
    skill = load_skill(p)
    assert skill.lineage == ("hermes/single-source",)


# ─── Cycle 4: discover walks <root>/<name>/SKILL.md ────────────


def test_discover_returns_all_skills(tmp_path: Path):
    _write_skill(tmp_path / "alpha", _frontmatter({**_MIN_FRONTMATTER, "name": "alpha"}))
    _write_skill(tmp_path / "beta", _frontmatter({**_MIN_FRONTMATTER, "name": "beta"}))
    _write_skill(tmp_path / "gamma", _frontmatter({**_MIN_FRONTMATTER, "name": "gamma"}))
    skills = discover_skills(tmp_path)
    names = sorted(s.name for s in skills)
    assert names == ["alpha", "beta", "gamma"]


def test_discover_returns_empty_for_empty_root(tmp_path: Path):
    assert discover_skills(tmp_path) == []


def test_discover_returns_empty_for_missing_root(tmp_path: Path):
    assert discover_skills(tmp_path / "does-not-exist") == []


# ─── Cycle 5: skip non-conforming dirs ────────────────────────


def test_discover_ignores_dirs_without_manifest(tmp_path: Path):
    (tmp_path / "no_manifest").mkdir()
    _write_skill(tmp_path / "real", _frontmatter({**_MIN_FRONTMATTER, "name": "real"}))
    skills = discover_skills(tmp_path)
    assert [s.name for s in skills] == ["real"]


def test_discover_ignores_top_level_files(tmp_path: Path):
    (tmp_path / "README.md").write_text("not a skill")
    _write_skill(tmp_path / "real", _frontmatter({**_MIN_FRONTMATTER, "name": "real"}))
    skills = discover_skills(tmp_path)
    assert [s.name for s in skills] == ["real"]


def test_discover_propagates_manifest_error(tmp_path: Path):
    """A broken SKILL.md should fail loudly, not silently skip."""
    _write_skill(tmp_path / "broken", "---\nname: only\n---\n")
    with pytest.raises(SkillManifestError):
        discover_skills(tmp_path)


# ─── Cycle 6: CLI lists skills ─────────────────────────────────


def test_cli_lists_discovered_skills(tmp_path: Path, monkeypatch):
    """`sera skills --root <path>` prints every skill it finds, with version."""
    from click.testing import CliRunner

    _write_skill(
        tmp_path / "caveman",
        _frontmatter(
            {
                "name": "caveman",
                "trigger": "/caveman",
                "permission": "READ",
                "version": "0.1.0",
            }
        ),
    )
    _write_skill(
        tmp_path / "egoist",
        _frontmatter(
            {
                "name": "egoist",
                "trigger": "/egoist",
                "permission": "READ",
                "version": "0.2.0",
                "lineage": ["hermes/egoist"],
            }
        ),
    )

    from sera.cli.main import main

    runner = CliRunner()
    result = runner.invoke(main, ["skills", "--root", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "caveman" in result.output
    assert "egoist" in result.output
    assert "0.1.0" in result.output
    assert "0.2.0" in result.output


def test_bundled_three_skills_discoverable():
    """Phase verification: 3 hand-written skills appear under tests/eval_cases/skills/."""
    bundle = Path(__file__).parent / "eval_cases" / "skills"
    skills = discover_skills(bundle)
    names = sorted(s.name for s in skills)
    assert names == ["caveman", "council", "egoist"]
    council = next(s for s in skills if s.name == "council")
    assert council.council is True
    assert council.args_schema is not None
    assert "llm-council/master" in council.lineage


def test_cli_reports_empty_when_no_skills(tmp_path: Path):
    from click.testing import CliRunner
    from sera.cli.main import main

    runner = CliRunner()
    result = runner.invoke(main, ["skills", "--root", str(tmp_path)])
    assert result.exit_code == 0
    assert "no skills" in result.output.lower() or "empty" in result.output.lower()
