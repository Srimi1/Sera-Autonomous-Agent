"""P-27: git-tracked skill history (TDD)."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from sera.skills.git import ensure_repo


def _git_present() -> bool:
    return subprocess.run(
        ["git", "--version"], capture_output=True, text=True
    ).returncode == 0


pytestmark = pytest.mark.skipif(not _git_present(), reason="git CLI missing")


def _write_skill_md(dir_: Path, name: str, body: str) -> Path:
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


# ─── Cycle 1: ensure_repo ─────────────────────────────────────


def test_ensure_repo_creates_git_on_first_call(tmp_path: Path):
    skills_dir = tmp_path / "skills"
    ensure_repo(skills_dir)
    assert (skills_dir / ".git").is_dir()


def test_ensure_repo_is_idempotent(tmp_path: Path):
    skills_dir = tmp_path / "skills"
    ensure_repo(skills_dir)
    head_first = (skills_dir / ".git" / "HEAD").read_text()
    ensure_repo(skills_dir)
    head_second = (skills_dir / ".git" / "HEAD").read_text()
    assert head_first == head_second


def test_ensure_repo_creates_dir_if_missing(tmp_path: Path):
    skills_dir = tmp_path / "fresh"
    ensure_repo(skills_dir)
    assert skills_dir.is_dir()
    assert (skills_dir / ".git").is_dir()


# ─── Cycle 2: commit_skill_change + skill_log ─────────────────


def test_commit_records_skill_md(tmp_path: Path):
    from sera.skills.git import commit_skill_change, skill_log

    skills_dir = tmp_path / "skills"
    _write_skill_md(skills_dir / "myskill", "myskill", body="v1")
    commit_skill_change(skills_dir, "myskill", "initial")
    log = skill_log(skills_dir, "myskill")
    assert len(log) == 1
    assert log[0].message == "initial"
    assert log[0].sha


def test_log_returns_chain_in_reverse_chronological_order(tmp_path: Path):
    from sera.skills.git import commit_skill_change, skill_log

    skills_dir = tmp_path / "skills"
    md_path = _write_skill_md(skills_dir / "evo", "evo", body="v1")
    commit_skill_change(skills_dir, "evo", "first edit")

    md_path.write_text(md_path.read_text().replace("v1", "v2"))
    commit_skill_change(skills_dir, "evo", "second edit")

    md_path.write_text(md_path.read_text().replace("v2", "v3"))
    commit_skill_change(skills_dir, "evo", "third edit")

    log = skill_log(skills_dir, "evo")
    assert [c.message for c in log] == ["third edit", "second edit", "first edit"]


def test_log_limit_caps_returned_count(tmp_path: Path):
    from sera.skills.git import commit_skill_change, skill_log

    skills_dir = tmp_path / "skills"
    md_path = _write_skill_md(skills_dir / "many", "many", body="v0")
    for i in range(5):
        md_path.write_text(md_path.read_text() + f"\nedit {i}")
        commit_skill_change(skills_dir, "many", f"edit {i}")
    assert len(skill_log(skills_dir, "many", limit=2)) == 2


def test_log_filters_to_named_skill(tmp_path: Path):
    """Commits touching other skills must not appear in this skill's log."""
    from sera.skills.git import commit_skill_change, skill_log

    skills_dir = tmp_path / "skills"
    _write_skill_md(skills_dir / "alpha", "alpha", body="a")
    _write_skill_md(skills_dir / "bravo", "bravo", body="b")
    commit_skill_change(skills_dir, "alpha", "alpha edit")
    commit_skill_change(skills_dir, "bravo", "bravo edit")
    log_a = skill_log(skills_dir, "alpha")
    assert [c.message for c in log_a] == ["alpha edit"]


def test_commit_with_no_changes_is_silent(tmp_path: Path):
    """Re-committing an unchanged SKILL.md must not error or add a duplicate."""
    from sera.skills.git import commit_skill_change, skill_log

    skills_dir = tmp_path / "skills"
    _write_skill_md(skills_dir / "stable", "stable", body="unchanged")
    commit_skill_change(skills_dir, "stable", "first")
    commit_skill_change(skills_dir, "stable", "second-noop")
    log = skill_log(skills_dir, "stable")
    assert len(log) == 1
    assert log[0].message == "first"


def test_log_on_unknown_skill_returns_empty(tmp_path: Path):
    from sera.skills.git import ensure_repo, skill_log

    skills_dir = tmp_path / "skills"
    ensure_repo(skills_dir)
    assert skill_log(skills_dir, "phantom") == []


def test_commit_custom_author(tmp_path: Path):
    from sera.skills.git import commit_skill_change, skill_log

    skills_dir = tmp_path / "skills"
    _write_skill_md(skills_dir / "auth", "auth", body="x")
    commit_skill_change(
        skills_dir, "auth", "curator nudge", author="curator <curator@sera>"
    )
    log = skill_log(skills_dir, "auth")
    assert log[0].author.startswith("curator")


# ─── Cycle 3: skill_diff ──────────────────────────────────────


def test_diff_shows_change_between_refs(tmp_path: Path):
    from sera.skills.git import commit_skill_change, skill_diff, skill_log

    skills_dir = tmp_path / "skills"
    md_path = _write_skill_md(skills_dir / "diff_me", "diff_me", body="alpha line")
    commit_skill_change(skills_dir, "diff_me", "v1")

    md_path.write_text(md_path.read_text().replace("alpha line", "beta line"))
    commit_skill_change(skills_dir, "diff_me", "v2")

    log = skill_log(skills_dir, "diff_me")
    diff = skill_diff(skills_dir, "diff_me", log[1].sha, log[0].sha)
    assert "-alpha line" in diff
    assert "+beta line" in diff


def test_diff_against_head_default(tmp_path: Path):
    """No ref_b → diff working tree (or HEAD~1..HEAD) — pick documented behavior."""
    from sera.skills.git import commit_skill_change, skill_diff

    skills_dir = tmp_path / "skills"
    md_path = _write_skill_md(skills_dir / "delta", "delta", body="one")
    commit_skill_change(skills_dir, "delta", "first")
    md_path.write_text(md_path.read_text().replace("one", "two"))
    commit_skill_change(skills_dir, "delta", "second")

    # Default is HEAD~1..HEAD when both refs omitted.
    diff = skill_diff(skills_dir, "delta")
    assert "-one" in diff
    assert "+two" in diff


def test_diff_unknown_skill_returns_empty_string(tmp_path: Path):
    from sera.skills.git import ensure_repo, skill_diff

    skills_dir = tmp_path / "skills"
    ensure_repo(skills_dir)
    assert skill_diff(skills_dir, "phantom") == ""


# ─── Cycle 4: CLI sera skills log / commit / diff ─────────────


def test_cli_skills_commit_then_log(tmp_path: Path):
    from click.testing import CliRunner

    from sera.cli.main import main

    skills_dir = tmp_path / "skills"
    _write_skill_md(skills_dir / "ledger", "ledger", body="entry one")

    runner = CliRunner()
    res_commit = runner.invoke(
        main,
        ["skills", "--root", str(skills_dir), "commit", "ledger",
         "--message", "first entry"],
    )
    assert res_commit.exit_code == 0, res_commit.output

    res_log = runner.invoke(
        main, ["skills", "--root", str(skills_dir), "log", "ledger"]
    )
    assert res_log.exit_code == 0, res_log.output
    assert "first entry" in res_log.output


def test_cli_skills_diff_shows_change(tmp_path: Path):
    from click.testing import CliRunner

    from sera.cli.main import main
    from sera.skills.git import commit_skill_change

    skills_dir = tmp_path / "skills"
    md_path = _write_skill_md(skills_dir / "evo", "evo", body="line one")
    commit_skill_change(skills_dir, "evo", "first")
    md_path.write_text(md_path.read_text().replace("line one", "line two"))
    commit_skill_change(skills_dir, "evo", "second")

    runner = CliRunner()
    result = runner.invoke(
        main, ["skills", "--root", str(skills_dir), "diff", "evo"]
    )
    assert result.exit_code == 0
    assert "line one" in result.output
    assert "line two" in result.output


def test_cli_skills_log_empty_message(tmp_path: Path):
    from click.testing import CliRunner
    from sera.cli.main import main
    from sera.skills.git import ensure_repo

    skills_dir = tmp_path / "skills"
    ensure_repo(skills_dir)
    runner = CliRunner()
    result = runner.invoke(
        main, ["skills", "--root", str(skills_dir), "log", "phantom"]
    )
    assert result.exit_code == 0
    assert "no history" in result.output.lower() or "no commits" in result.output.lower()
