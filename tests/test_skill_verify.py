"""P-25: skill replay verification (TDD)."""
from __future__ import annotations

from pathlib import Path

import pytest

from sera.skills.lifecycle import SkillLifecycle


# ─── Cycle 1: verified flag + mark_candidate ──────────────────


def test_new_skill_defaults_verified_for_backcompat(tmp_path: Path):
    """Pre-P-25 behaviour: new rows are verified=True so existing flows don't break."""
    lc = SkillLifecycle(db_path=tmp_path / "lc.db")
    lc.upsert("legacy", now=100.0)
    assert lc.is_verified("legacy") is True


def test_mark_candidate_sets_verified_false(tmp_path: Path):
    lc = SkillLifecycle(db_path=tmp_path / "lc.db")
    lc.upsert("new-skill", now=100.0)
    lc.mark_candidate("new-skill")
    assert lc.is_verified("new-skill") is False


def test_mark_candidate_creates_row_when_unseen(tmp_path: Path):
    """Candidate marker on an unseen skill should establish the row."""
    lc = SkillLifecycle(db_path=tmp_path / "lc.db")
    lc.mark_candidate("brand-new", now=42.0)
    row = lc.get("brand-new")
    assert row is not None
    assert lc.is_verified("brand-new") is False


def test_is_verified_unknown_skill_returns_true(tmp_path: Path):
    """Unknown skills implicitly read as verified — same default contract."""
    lc = SkillLifecycle(db_path=tmp_path / "lc.db")
    assert lc.is_verified("phantom") is True


# ─── Cycle 2: verify() roundtrip ──────────────────────────────


def test_verify_flips_candidate_to_verified(tmp_path: Path):
    lc = SkillLifecycle(db_path=tmp_path / "lc.db")
    lc.mark_candidate("aspirant", now=10.0)
    assert lc.is_verified("aspirant") is False
    lc.verify("aspirant", now=20.0)
    assert lc.is_verified("aspirant") is True


def test_verify_is_idempotent(tmp_path: Path):
    lc = SkillLifecycle(db_path=tmp_path / "lc.db")
    lc.verify("twice")
    lc.verify("twice")
    assert lc.is_verified("twice") is True


def test_lifecycle_row_exposes_verified(tmp_path: Path):
    from sera.skills.lifecycle import LifecycleRow

    lc = SkillLifecycle(db_path=tmp_path / "lc.db")
    lc.mark_candidate("c")
    row = lc.get("c")
    assert isinstance(row, LifecycleRow)
    assert row.verified is False
    lc.verify("c")
    assert lc.get("c").verified is True


# ─── Cycle 3: SkillRegistry skips unverified candidates ───────


def _write_skill_file(dir_: Path, name: str, body: str = "body") -> Path:
    dir_.mkdir(parents=True, exist_ok=True)
    p = dir_ / "SKILL.md"
    p.write_text(
        "---\n"
        f"name: {name}\n"
        "trigger: /x\n"
        "permission: READ_ONLY\n"
        "version: 0.1.0\n"
        "---\n"
        f"{body}\n"
    )
    return p


def test_registry_skips_unverified_candidate(tmp_path: Path):
    """Broken/new candidate skill must NOT register as a tool."""
    from sera.skills.loader import SkillRegistry
    from sera.tools import registry as tool_registry

    _write_skill_file(tmp_path / "trusted", "trusted")
    _write_skill_file(tmp_path / "candidate", "candidate")

    lc = SkillLifecycle(db_path=tmp_path / "lc.db")
    lc.mark_candidate("candidate")

    reg = SkillRegistry(root=tmp_path, lifecycle=lc)
    summary = reg.refresh()

    assert "trusted" in summary.added
    assert "candidate" not in summary.added
    assert tool_registry.get("skill.trusted") is not None
    assert tool_registry.get("skill.candidate") is None
    reg.clear()


def test_registry_picks_up_skill_after_verification(tmp_path: Path):
    """End-to-end: candidate → verify → next refresh registers."""
    from sera.skills.loader import SkillRegistry
    from sera.tools import registry as tool_registry

    _write_skill_file(tmp_path / "pending", "pending")
    lc = SkillLifecycle(db_path=tmp_path / "lc.db")
    lc.mark_candidate("pending")

    reg = SkillRegistry(root=tmp_path, lifecycle=lc)
    reg.refresh()
    assert tool_registry.get("skill.pending") is None

    lc.verify("pending")
    reg.refresh()
    assert tool_registry.get("skill.pending") is not None
    reg.clear()


def test_pinned_candidate_still_registers(tmp_path: Path):
    """Pin overrides candidate gate — user-overridden trust."""
    from sera.skills.loader import SkillRegistry
    from sera.tools import registry as tool_registry

    _write_skill_file(tmp_path / "override", "override")
    lc = SkillLifecycle(db_path=tmp_path / "lc.db")
    lc.mark_candidate("override")
    lc.pin("override")

    reg = SkillRegistry(root=tmp_path, lifecycle=lc)
    reg.refresh()
    assert tool_registry.get("skill.override") is not None
    reg.clear()


# ─── Cycle 4: ReplayCase + replay_skill ───────────────────────


def test_replay_case_substring_match_passes(tmp_path: Path):
    import asyncio

    from sera.skills.loader import load_skill
    from sera.skills.verify import ReplayCase, replay_skill

    p = _write_skill_file(tmp_path / "echo", "echo", body="hello world matched")
    skill = load_skill(p)
    case = ReplayCase(id="c1", input={}, expect_substring="matched")
    result = asyncio.run(replay_skill(skill, case))
    assert result.passed is True
    assert result.reason == ""


def test_replay_case_substring_mismatch_fails(tmp_path: Path):
    import asyncio

    from sera.skills.loader import load_skill
    from sera.skills.verify import ReplayCase, replay_skill

    p = _write_skill_file(tmp_path / "echo", "echo", body="hello world")
    skill = load_skill(p)
    case = ReplayCase(id="c1", input={}, expect_substring="not-present")
    result = asyncio.run(replay_skill(skill, case))
    assert result.passed is False
    assert "not-present" in result.reason


def test_replay_case_no_expectations_is_pass(tmp_path: Path):
    """A case with no expectations just verifies the handler didn't raise."""
    import asyncio
    from sera.skills.loader import load_skill
    from sera.skills.verify import ReplayCase, replay_skill

    p = _write_skill_file(tmp_path / "smoke", "smoke", body="anything goes")
    skill = load_skill(p)
    case = ReplayCase(id="smoke", input={})
    result = asyncio.run(replay_skill(skill, case))
    assert result.passed is True


def test_replay_handler_exception_fails_loudly():
    """A handler that raises must produce a failing ReplayResult, never propagate."""
    import asyncio

    from sera.skills.verify import ReplayCase, replay_tool
    from sera.tools.base import Permission, Tool, ToolScope

    async def explode(_args, _ctx):
        raise RuntimeError("simulated handler crash")

    tool = Tool(
        name="skill.boom",
        description="boom",
        parameters={"type": "object"},
        permission=Permission.READ_ONLY,
        scope=ToolScope.SKILL,
        handler=explode,
    )
    result = asyncio.run(replay_tool(tool, ReplayCase(id="boom", input={})))
    assert result.passed is False
    assert "simulated handler crash" in result.reason


def test_replay_case_expect_equals(tmp_path: Path):
    """Exact-match expectation — stricter than substring."""
    import asyncio
    from sera.skills.loader import load_skill
    from sera.skills.verify import ReplayCase, replay_skill

    p = _write_skill_file(tmp_path / "exact", "exact", body="precise output")
    skill = load_skill(p)
    case_ok = ReplayCase(id="ok", input={}, expect_equals="precise output")
    case_bad = ReplayCase(id="bad", input={}, expect_equals="other")
    assert asyncio.run(replay_skill(skill, case_ok)).passed is True
    assert asyncio.run(replay_skill(skill, case_bad)).passed is False


# ─── Cycle 5: verify_via_replay flips lifecycle ───────────────


def test_verify_via_replay_promotes_when_all_pass(tmp_path: Path):
    import asyncio
    from sera.skills.loader import load_skill
    from sera.skills.verify import ReplayCase, verify_via_replay

    p = _write_skill_file(tmp_path / "promote", "promote", body="contains the word success")
    skill = load_skill(p)
    lc = SkillLifecycle(db_path=tmp_path / "lc.db")
    lc.mark_candidate("promote")
    assert lc.is_verified("promote") is False

    cases = [
        ReplayCase(id="c1", input={}, expect_substring="contains"),
        ReplayCase(id="c2", input={}, expect_substring="success"),
    ]
    report = asyncio.run(verify_via_replay(lc, skill, cases))

    assert report.passed is True
    assert report.n_passed == 2
    assert lc.is_verified("promote") is True


def test_verify_via_replay_keeps_candidate_when_any_fails(tmp_path: Path):
    """Phase verification clause: broken skill stays in candidate."""
    import asyncio
    from sera.skills.loader import load_skill
    from sera.skills.verify import ReplayCase, verify_via_replay

    p = _write_skill_file(tmp_path / "broken", "broken", body="only this text")
    skill = load_skill(p)
    lc = SkillLifecycle(db_path=tmp_path / "lc.db")
    lc.mark_candidate("broken")

    cases = [
        ReplayCase(id="ok", input={}, expect_substring="only"),
        ReplayCase(id="fail", input={}, expect_substring="not-there"),
    ]
    report = asyncio.run(verify_via_replay(lc, skill, cases))

    assert report.passed is False
    assert report.n_failed == 1
    assert lc.is_verified("broken") is False


def test_verify_via_replay_empty_case_list_does_not_promote(tmp_path: Path):
    """A skill with no captured replay cases can't be promoted by accident."""
    import asyncio
    from sera.skills.loader import load_skill
    from sera.skills.verify import verify_via_replay

    p = _write_skill_file(tmp_path / "untested", "untested")
    skill = load_skill(p)
    lc = SkillLifecycle(db_path=tmp_path / "lc.db")
    lc.mark_candidate("untested")
    report = asyncio.run(verify_via_replay(lc, skill, []))
    assert report.passed is False
    assert lc.is_verified("untested") is False


# ─── Cycle 6: YAML loader + bundled traces ────────────────────


def test_load_replay_cases_parses_yaml():
    from sera.skills.verify import load_replay_cases, load_replay_skill_name

    p = Path(__file__).parent / "skill_replay" / "caveman.yaml"
    assert load_replay_skill_name(p) == "caveman"
    cases = load_replay_cases(p)
    assert len(cases) == 2
    assert cases[0].id == "cav_signal_substring"
    assert cases[0].expect_substring == "Caveman mode"


def test_load_replay_cases_handles_missing_expect():
    """A case with no `expect` block parses as a smoke-only check."""
    from sera.skills.verify import ReplayCase, load_replay_cases

    import tempfile
    with tempfile.NamedTemporaryFile(
        "w", suffix=".yaml", delete=False
    ) as f:
        f.write("skill: x\ncases:\n  - id: smoke\n    input: {}\n")
        path = Path(f.name)
    cases = load_replay_cases(path)
    assert cases == [ReplayCase(id="smoke", input={})]


def test_bundled_caveman_replay_verifies_against_real_skill():
    """End-to-end: bundled SKILL.md + bundled replay yaml → verified."""
    import asyncio
    from sera.skills.loader import load_skill
    from sera.skills.verify import (
        load_replay_cases,
        load_replay_skill_name,
        verify_via_replay,
    )

    skills_dir = Path(__file__).parent / "eval_cases" / "skills" / "caveman"
    replay_path = Path(__file__).parent / "skill_replay" / "caveman.yaml"
    skill = load_skill(skills_dir / "SKILL.md")
    assert load_replay_skill_name(replay_path) == skill.name

    import tempfile
    with tempfile.TemporaryDirectory() as td:
        lc = SkillLifecycle(db_path=Path(td) / "lc.db")
        lc.mark_candidate(skill.name)
        report = asyncio.run(
            verify_via_replay(lc, skill, load_replay_cases(replay_path))
        )
        assert report.passed is True
        assert lc.is_verified(skill.name) is True
