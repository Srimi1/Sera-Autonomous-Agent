"""P-24: skill lifecycle state machine (TDD)."""
from __future__ import annotations

from pathlib import Path

import pytest

from sera.skills.lifecycle import (
    LifecycleState,
    SkillLifecycle,
)


DAY = 24 * 60 * 60


# ─── Cycle 1: tracer ───────────────────────────────────────────


def test_unseen_skill_is_active(tmp_path: Path):
    lc = SkillLifecycle(db_path=tmp_path / "lc.db")
    assert lc.state_of("brand-new") is LifecycleState.ACTIVE


def test_upsert_then_state_of_returns_active(tmp_path: Path):
    lc = SkillLifecycle(db_path=tmp_path / "lc.db")
    lc.upsert("first-touch", now=1000.0)
    assert lc.state_of("first-touch", now=1000.0) is LifecycleState.ACTIVE


# ─── Cycle 2: touch bumps last_used_at ────────────────────────


def test_touch_updates_last_used_at(tmp_path: Path):
    lc = SkillLifecycle(db_path=tmp_path / "lc.db")
    lc.upsert("worker", now=1000.0)
    lc.touch("worker", now=2000.0)
    row = lc.get("worker")
    assert row is not None
    assert row.last_used_at == pytest.approx(2000.0)


def test_touch_creates_row_for_unseen(tmp_path: Path):
    """First-touch behaviour — touch can stand in for upsert."""
    lc = SkillLifecycle(db_path=tmp_path / "lc.db")
    lc.touch("fresh", now=5000.0)
    row = lc.get("fresh")
    assert row is not None
    assert row.last_used_at == pytest.approx(5000.0)
    assert row.state is LifecycleState.ACTIVE


def test_get_returns_none_for_unknown(tmp_path: Path):
    lc = SkillLifecycle(db_path=tmp_path / "lc.db")
    assert lc.get("ghost") is None


# ─── Cycle 3: idle 90d → STALE; touch returns from STALE ──────


def test_idle_90_days_reads_as_stale(tmp_path: Path):
    lc = SkillLifecycle(db_path=tmp_path / "lc.db")
    start = 1_000_000.0
    lc.upsert("old", now=start)
    later = start + 90 * DAY
    assert lc.state_of("old", now=later) is LifecycleState.STALE


def test_idle_just_under_90_days_still_active(tmp_path: Path):
    lc = SkillLifecycle(db_path=tmp_path / "lc.db")
    start = 1_000_000.0
    lc.upsert("borderline", now=start)
    almost = start + 89 * DAY
    assert lc.state_of("borderline", now=almost) is LifecycleState.ACTIVE


def test_touch_recovers_from_stale(tmp_path: Path):
    lc = SkillLifecycle(db_path=tmp_path / "lc.db")
    start = 1_000_000.0
    lc.upsert("comeback", now=start)
    # Decays to STALE.
    distant = start + 100 * DAY
    assert lc.state_of("comeback", now=distant) is LifecycleState.STALE
    # Touch + re-read at same `distant` → ACTIVE.
    lc.touch("comeback", now=distant)
    assert lc.state_of("comeback", now=distant) is LifecycleState.ACTIVE


# ─── Cycle 4: sweep proposes archives past 180d ───────────────


def test_sweep_returns_archive_proposals(tmp_path: Path):
    lc = SkillLifecycle(db_path=tmp_path / "lc.db")
    start = 1_000_000.0
    lc.upsert("fresh", now=start)
    lc.upsert("stale_only", now=start - 100 * DAY)
    lc.upsert("archive_candidate", now=start - 200 * DAY)
    summary = lc.sweep(now=start)
    assert "archive_candidate" in summary.proposed_archives
    assert "stale_only" not in summary.proposed_archives
    assert "fresh" not in summary.proposed_archives


def test_sweep_does_not_auto_archive(tmp_path: Path):
    """Archive is user-confirmed; sweep must never apply it directly."""
    lc = SkillLifecycle(db_path=tmp_path / "lc.db")
    start = 1_000_000.0
    lc.upsert("aged", now=start - 200 * DAY)
    lc.sweep(now=start)
    assert lc.state_of("aged", now=start) is not LifecycleState.ARCHIVED


def test_sweep_reports_stale_transitions(tmp_path: Path):
    lc = SkillLifecycle(db_path=tmp_path / "lc.db")
    start = 1_000_000.0
    lc.upsert("a", now=start - 95 * DAY)
    lc.upsert("b", now=start)  # still active
    summary = lc.sweep(now=start)
    assert "a" in summary.transitions_to_stale
    assert "b" not in summary.transitions_to_stale


def test_sweep_idempotent_on_already_stale(tmp_path: Path):
    """Re-sweeping shouldn't report the same skill as 'newly stale' twice."""
    lc = SkillLifecycle(db_path=tmp_path / "lc.db")
    start = 1_000_000.0
    lc.upsert("already_stale", now=start - 100 * DAY)
    first = lc.sweep(now=start)
    second = lc.sweep(now=start)
    assert "already_stale" in first.transitions_to_stale
    assert "already_stale" not in second.transitions_to_stale


# ─── Cycle 5: pin / unpin + PINNED never auto-transitions ─────


def test_pin_overrides_decay(tmp_path: Path):
    lc = SkillLifecycle(db_path=tmp_path / "lc.db")
    start = 1_000_000.0
    lc.upsert("vip", now=start - 500 * DAY)
    lc.pin("vip")
    assert lc.state_of("vip", now=start) is LifecycleState.PINNED
    summary = lc.sweep(now=start)
    assert "vip" not in summary.transitions_to_stale
    assert "vip" not in summary.proposed_archives


def test_pin_unseen_skill_creates_row(tmp_path: Path):
    lc = SkillLifecycle(db_path=tmp_path / "lc.db")
    lc.pin("brand-new", now=42.0)
    row = lc.get("brand-new")
    assert row is not None and row.pinned is True


def test_unpin_lets_decay_resume(tmp_path: Path):
    lc = SkillLifecycle(db_path=tmp_path / "lc.db")
    start = 1_000_000.0
    lc.upsert("toggling", now=start - 100 * DAY)
    lc.pin("toggling")
    assert lc.state_of("toggling", now=start) is LifecycleState.PINNED
    lc.unpin("toggling")
    assert lc.state_of("toggling", now=start) is LifecycleState.STALE


# ─── Cycle 6: archive + revive ────────────────────────────────


def test_archive_flips_state_and_records_timestamp(tmp_path: Path):
    lc = SkillLifecycle(db_path=tmp_path / "lc.db")
    lc.upsert("retire-me", now=100.0)
    lc.archive("retire-me", now=200.0)
    row = lc.get("retire-me")
    assert row.state is LifecycleState.ARCHIVED
    assert row.archived_at == pytest.approx(200.0)


def test_archived_is_sticky_against_decay(tmp_path: Path):
    """An archived skill stays archived regardless of age."""
    lc = SkillLifecycle(db_path=tmp_path / "lc.db")
    lc.upsert("museum", now=0.0)
    lc.archive("museum", now=1.0)
    far_future = 10_000 * DAY
    assert lc.state_of("museum", now=far_future) is LifecycleState.ARCHIVED


def test_revive_returns_archived_to_active(tmp_path: Path):
    """Outclass: archived skills are never deleted; revive flips them back."""
    lc = SkillLifecycle(db_path=tmp_path / "lc.db")
    lc.upsert("phoenix", now=0.0)
    lc.archive("phoenix", now=1.0)
    assert lc.state_of("phoenix", now=2.0) is LifecycleState.ARCHIVED

    lc.revive("phoenix", now=100.0)
    assert lc.state_of("phoenix", now=100.0) is LifecycleState.ACTIVE
    row = lc.get("phoenix")
    assert row.archived_at is None
    assert row.last_used_at == pytest.approx(100.0)


def test_revive_unknown_is_noop(tmp_path: Path):
    lc = SkillLifecycle(db_path=tmp_path / "lc.db")
    lc.revive("ghost")  # must not raise; row still absent
    assert lc.get("ghost") is None


def test_archive_overrides_pin(tmp_path: Path):
    """Explicit archive on a pinned skill wins — user is in charge."""
    lc = SkillLifecycle(db_path=tmp_path / "lc.db")
    lc.pin("hot", now=0.0)
    lc.archive("hot", now=1.0)
    assert lc.state_of("hot", now=2.0) is LifecycleState.ARCHIVED


# ─── Cycle 7: SkillRegistry integration ───────────────────────


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


def test_registry_skips_archived_skills(tmp_path: Path):
    """A skill flagged ARCHIVED in the lifecycle DB must not register as a tool."""
    from sera.skills.loader import SkillRegistry
    from sera.tools import registry as tool_registry

    _write_skill_file(tmp_path / "alive", "alive")
    _write_skill_file(tmp_path / "dead", "dead")

    lc = SkillLifecycle(db_path=tmp_path / "lc.db")
    lc.archive("dead", now=10.0)

    reg = SkillRegistry(root=tmp_path, lifecycle=lc)
    summary = reg.refresh()

    assert "alive" in summary.added
    assert "dead" not in summary.added
    assert tool_registry.get("skill.alive") is not None
    assert tool_registry.get("skill.dead") is None
    reg.clear()


def test_registry_touches_lifecycle_on_register(tmp_path: Path):
    """Every successful refresh registration bumps the lifecycle row."""
    from sera.skills.loader import SkillRegistry

    _write_skill_file(tmp_path / "touched", "touched")
    lc = SkillLifecycle(db_path=tmp_path / "lc.db")
    reg = SkillRegistry(root=tmp_path, lifecycle=lc)
    reg.refresh()

    row = lc.get("touched")
    assert row is not None
    assert row.state is LifecycleState.ACTIVE
    reg.clear()


def test_registry_revived_skill_registers_again(tmp_path: Path):
    """End-to-end: archive → revive → next refresh re-registers the tool."""
    from sera.skills.loader import SkillRegistry
    from sera.tools import registry as tool_registry

    _write_skill_file(tmp_path / "comeback", "comeback")
    lc = SkillLifecycle(db_path=tmp_path / "lc.db")

    lc.archive("comeback", now=0.0)
    reg = SkillRegistry(root=tmp_path, lifecycle=lc)
    reg.refresh()
    assert tool_registry.get("skill.comeback") is None

    lc.revive("comeback")
    reg.refresh()
    assert tool_registry.get("skill.comeback") is not None
    reg.clear()
