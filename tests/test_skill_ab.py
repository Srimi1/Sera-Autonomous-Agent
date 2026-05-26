"""P-26: skill A/B harness — cost × success-rate fitness (TDD)."""
from __future__ import annotations

from pathlib import Path

import pytest

from sera.skills.ab import ABResult, compute_verdict
from sera.skills.lifecycle import SkillLifecycle


# ─── Cycle 1: compute_verdict lex math ─────────────────────────


def _result(name: str, passed: int, total: int, cost: float) -> ABResult:
    return ABResult(
        name=name, n_passed=passed, total_cases=total, total_cost=cost,
    )


def test_higher_success_rate_wins_regardless_of_cost():
    a = _result("a", passed=3, total=5, cost=0.10)   # 60% success, cheap
    b = _result("b", passed=4, total=5, cost=10.00)  # 80% success, expensive
    v = compute_verdict(a, b)
    assert v.winner == "b"
    assert v.loser == "a"
    assert "success" in v.reason.lower()


def test_tie_in_success_cheaper_wins():
    a = _result("a", passed=5, total=5, cost=2.0)
    b = _result("b", passed=5, total=5, cost=1.0)
    v = compute_verdict(a, b)
    assert v.winner == "b"
    assert "cost" in v.reason.lower()


def test_total_tie_first_arg_wins():
    """Stable order — A wins ties to make verdicts deterministic in tests."""
    a = _result("a", passed=5, total=5, cost=1.0)
    b = _result("b", passed=5, total=5, cost=1.0)
    v = compute_verdict(a, b)
    assert v.winner == "a"
    assert v.loser == "b"


# ─── Cycle 2: run_ab orchestrator ─────────────────────────────


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


def test_run_ab_both_pass_picks_cheaper(tmp_path: Path):
    """Both variants match the substring → cheaper wins."""
    import asyncio
    from sera.skills.ab import Variant, run_ab
    from sera.skills.loader import load_skill
    from sera.skills.verify import ReplayCase

    skill_a = load_skill(
        _write_skill_file(tmp_path / "a", "ver_a", body="hello world")
    )
    skill_b = load_skill(
        _write_skill_file(tmp_path / "b", "ver_b", body="hello world")
    )
    cases = [ReplayCase(id="c1", input={}, expect_substring="hello")]

    result_a, result_b, verdict = asyncio.run(
        run_ab(Variant(skill=skill_a, cost=2.0), Variant(skill=skill_b, cost=1.0), cases)
    )
    assert result_a.n_passed == 1
    assert result_b.n_passed == 1
    assert verdict.winner == "ver_b"


def test_run_ab_higher_success_wins(tmp_path: Path):
    import asyncio
    from sera.skills.ab import Variant, run_ab
    from sera.skills.loader import load_skill
    from sera.skills.verify import ReplayCase

    skill_a = load_skill(
        _write_skill_file(tmp_path / "a", "ver_a", body="alpha")
    )
    skill_b = load_skill(
        _write_skill_file(tmp_path / "b", "ver_b", body="alpha beta")
    )
    cases = [
        ReplayCase(id="c1", input={}, expect_substring="alpha"),
        ReplayCase(id="c2", input={}, expect_substring="beta"),
    ]
    _, _, verdict = asyncio.run(
        run_ab(
            Variant(skill=skill_a, cost=0.01),  # cheaper but fails c2
            Variant(skill=skill_b, cost=100.0),
            cases,
        )
    )
    assert verdict.winner == "ver_b"
    assert "success" in verdict.reason.lower()


def test_run_ab_no_cases_returns_zero_pass(tmp_path: Path):
    import asyncio
    from sera.skills.ab import Variant, run_ab
    from sera.skills.loader import load_skill

    sa = load_skill(_write_skill_file(tmp_path / "a", "a"))
    sb = load_skill(_write_skill_file(tmp_path / "b", "b"))
    result_a, result_b, verdict = asyncio.run(
        run_ab(Variant(sa, cost=1), Variant(sb, cost=2), [])
    )
    assert result_a.n_passed == 0 and result_a.total_cases == 0
    assert result_b.n_passed == 0 and result_b.total_cases == 0
    assert verdict.winner == "a"  # tie, first-arg-wins


def test_variant_cost_summed_across_cases(tmp_path: Path):
    """total_cost in ABResult = per-call cost × n_cases run."""
    import asyncio
    from sera.skills.ab import Variant, run_ab
    from sera.skills.loader import load_skill
    from sera.skills.verify import ReplayCase

    sa = load_skill(_write_skill_file(tmp_path / "a", "a", body="x"))
    sb = load_skill(_write_skill_file(tmp_path / "b", "b", body="x"))
    cases = [
        ReplayCase(id=f"c{i}", input={}, expect_substring="x") for i in range(3)
    ]
    ra, rb, _ = asyncio.run(
        run_ab(Variant(sa, cost=2.0), Variant(sb, cost=5.0), cases)
    )
    assert ra.total_cost == pytest.approx(6.0)
    assert rb.total_cost == pytest.approx(15.0)


# ─── Cycle 3: decide_and_persist flips lifecycle ──────────────


def test_decide_and_persist_verifies_winner_archives_loser(tmp_path: Path):
    import asyncio
    from sera.skills.ab import Variant, decide_and_persist
    from sera.skills.lifecycle import LifecycleState
    from sera.skills.loader import load_skill
    from sera.skills.verify import ReplayCase

    sa = load_skill(_write_skill_file(tmp_path / "a", "ver_a", body="hello"))
    sb = load_skill(_write_skill_file(tmp_path / "b", "ver_b", body="hello"))
    lc = SkillLifecycle(db_path=tmp_path / "lc.db")
    lc.mark_candidate("ver_a")
    lc.mark_candidate("ver_b")
    cases = [ReplayCase(id="c1", input={}, expect_substring="hello")]
    verdict = asyncio.run(
        decide_and_persist(
            lc,
            Variant(sa, cost=2.0),
            Variant(sb, cost=1.0),  # cheaper → wins
            cases,
        )
    )
    assert verdict.winner == "ver_b"
    assert lc.is_verified("ver_b") is True
    assert lc.state_of("ver_a") is LifecycleState.ARCHIVED


def test_decide_and_persist_skips_when_neither_passes(tmp_path: Path):
    """If both variants fail every case, neither is promoted nor archived."""
    import asyncio
    from sera.skills.ab import Variant, decide_and_persist
    from sera.skills.lifecycle import LifecycleState
    from sera.skills.loader import load_skill
    from sera.skills.verify import ReplayCase

    sa = load_skill(_write_skill_file(tmp_path / "a", "ver_a", body="alpha"))
    sb = load_skill(_write_skill_file(tmp_path / "b", "ver_b", body="alpha"))
    lc = SkillLifecycle(db_path=tmp_path / "lc.db")
    lc.mark_candidate("ver_a")
    lc.mark_candidate("ver_b")
    cases = [ReplayCase(id="c1", input={}, expect_substring="nope")]
    verdict = asyncio.run(
        decide_and_persist(lc, Variant(sa, cost=1.0), Variant(sb, cost=2.0), cases)
    )
    # Verdict still returned for telemetry — but no lifecycle change.
    assert verdict is not None
    assert lc.is_verified("ver_a") is False
    assert lc.is_verified("ver_b") is False
    assert lc.state_of("ver_a") is not LifecycleState.ARCHIVED
    assert lc.state_of("ver_b") is not LifecycleState.ARCHIVED


def test_decide_and_persist_loser_recoverable_via_revive(tmp_path: Path):
    """Outclass: archived loser stays revivable — user override path lives."""
    import asyncio
    from sera.skills.ab import Variant, decide_and_persist
    from sera.skills.lifecycle import LifecycleState
    from sera.skills.loader import load_skill
    from sera.skills.verify import ReplayCase

    sa = load_skill(_write_skill_file(tmp_path / "a", "ver_a", body="hi"))
    sb = load_skill(_write_skill_file(tmp_path / "b", "ver_b", body="hi"))
    lc = SkillLifecycle(db_path=tmp_path / "lc.db")
    lc.mark_candidate("ver_a")
    lc.mark_candidate("ver_b")
    cases = [ReplayCase(id="c1", input={}, expect_substring="hi")]
    asyncio.run(
        decide_and_persist(lc, Variant(sa, cost=99), Variant(sb, cost=1), cases)
    )
    assert lc.state_of("ver_a") is LifecycleState.ARCHIVED
    lc.revive("ver_a")
    assert lc.state_of("ver_a") is LifecycleState.ACTIVE


# ─── Cycle 4: sera skills ab CLI ──────────────────────────────


def test_cli_skills_ab_picks_cheaper_winner(tmp_path: Path):
    """End-to-end: bundle two SKILL.md + a replay yaml → CLI picks cheaper."""
    from click.testing import CliRunner

    from sera.cli.main import main

    _write_skill_file(tmp_path / "ver_a", "ver_a", body="hello world")
    _write_skill_file(tmp_path / "ver_b", "ver_b", body="hello world")
    replay_yaml = tmp_path / "replay.yaml"
    replay_yaml.write_text(
        "skill: shared\n"
        "cases:\n"
        "  - id: c1\n"
        "    input: {}\n"
        "    expect:\n"
        "      substring: hello\n"
    )

    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "skills", "ab",
            "--a", str(tmp_path / "ver_a" / "SKILL.md"),
            "--b", str(tmp_path / "ver_b" / "SKILL.md"),
            "--cases", str(replay_yaml),
            "--cost-a", "2.0",
            "--cost-b", "1.0",
            "--lifecycle-db", str(tmp_path / "lc.db"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "ver_b" in result.output
    assert "winner" in result.output.lower()
