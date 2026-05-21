"""P-29: skill quality scoring (TDD)."""
from __future__ import annotations

from pathlib import Path

import pytest

from sera.skills.scoring import (
    DEFAULT_SUGGEST_THRESHOLD,
    SkillScore,
    SkillScorer,
    quality_score,
)


# ─── Cycle 1: quality_score() math ───────────────────────────────


def test_perfect_skill_scores_one():
    s = SkillScore(name="a", invocations=10, successes=10, failures=0,
                   total_cost=0.0, thumbs_up=5, thumbs_down=0)
    assert quality_score(s) == pytest.approx(1.0)


def test_zero_success_scores_low():
    s = SkillScore(name="b", invocations=5, successes=0, failures=5,
                   total_cost=0.0, thumbs_up=0, thumbs_down=3)
    assert quality_score(s) < 0.2


def test_new_skill_no_invocations_gets_benefit_of_doubt():
    s = SkillScore(name="c", invocations=0, successes=0, failures=0,
                   total_cost=0.0, thumbs_up=0, thumbs_down=0)
    assert quality_score(s) >= DEFAULT_SUGGEST_THRESHOLD


def test_thumbs_down_lowers_score():
    base = SkillScore(name="d", invocations=10, successes=8, failures=2,
                      total_cost=0.0, thumbs_up=0, thumbs_down=0)
    bad = SkillScore(name="d", invocations=10, successes=8, failures=2,
                     total_cost=0.0, thumbs_up=0, thumbs_down=5)
    assert quality_score(bad) < quality_score(base)


def test_thumbs_up_raises_score():
    base = SkillScore(name="e", invocations=10, successes=7, failures=3,
                      total_cost=0.0, thumbs_up=0, thumbs_down=0)
    good = SkillScore(name="e", invocations=10, successes=7, failures=3,
                      total_cost=0.0, thumbs_up=5, thumbs_down=0)
    assert quality_score(good) > quality_score(base)


def test_score_bounded_zero_to_one():
    for s in [
        SkillScore("x", 0, 0, 100, 999.9, 0, 99),
        SkillScore("x", 100, 100, 0, 0.0, 50, 0),
    ]:
        q = quality_score(s)
        assert 0.0 <= q <= 1.0


# ─── Cycle 2: SkillScorer store ───────────────────────────────────


def test_scorer_new_skill_returns_default_score(tmp_path: Path):
    sc = SkillScorer(db_path=tmp_path / "scores.db")
    assert sc.score_of("unknown") == quality_score(SkillScore("unknown"))


def test_scorer_record_success_improves_score(tmp_path: Path):
    sc = SkillScorer(db_path=tmp_path / "scores.db")
    sc.record_invocation("alpha")
    sc.record_failure("alpha")
    before = sc.score_of("alpha")
    sc.record_invocation("alpha")
    sc.record_success("alpha")
    after = sc.score_of("alpha")
    assert after > before


def test_scorer_record_failure_lowers_score(tmp_path: Path):
    sc = SkillScorer(db_path=tmp_path / "scores.db")
    for _ in range(3):
        sc.record_invocation("beta")
        sc.record_success("beta")
    good = sc.score_of("beta")
    sc.record_invocation("beta")
    sc.record_failure("beta")
    assert sc.score_of("beta") < good


def test_scorer_thumbs_up_persists(tmp_path: Path):
    sc = SkillScorer(db_path=tmp_path / "scores.db")
    sc.thumbs_up("gamma")
    row = sc.get("gamma")
    assert row.thumbs_up == 1


def test_scorer_thumbs_down_persists(tmp_path: Path):
    sc = SkillScorer(db_path=tmp_path / "scores.db")
    sc.thumbs_down("delta")
    row = sc.get("delta")
    assert row.thumbs_down == 1


def test_scorer_get_returns_score_dataclass(tmp_path: Path):
    sc = SkillScorer(db_path=tmp_path / "scores.db")
    sc.record_invocation("epsilon")
    sc.record_cost("epsilon", 0.05)
    row = sc.get("epsilon")
    assert isinstance(row, SkillScore)
    assert row.invocations == 1
    assert row.total_cost == pytest.approx(0.05)


# ─── Cycle 3: demotion ────────────────────────────────────────────


def test_three_failures_drops_below_threshold(tmp_path: Path):
    sc = SkillScorer(db_path=tmp_path / "scores.db")
    for _ in range(3):
        sc.record_invocation("bad_skill")
        sc.record_failure("bad_skill")
    assert sc.should_suggest("bad_skill") is False


def test_should_suggest_true_for_new_skill(tmp_path: Path):
    sc = SkillScorer(db_path=tmp_path / "scores.db")
    assert sc.should_suggest("brand_new") is True


def test_demoted_skills_returns_below_threshold(tmp_path: Path):
    sc = SkillScorer(db_path=tmp_path / "scores.db")
    for name in ("bad_a", "bad_b"):
        for _ in range(5):
            sc.record_invocation(name)
            sc.record_failure(name)
    sc.record_invocation("good")
    sc.record_success("good")
    demoted = sc.demoted_skills()
    assert "bad_a" in demoted
    assert "bad_b" in demoted
    assert "good" not in demoted


def test_recovery_from_demotion(tmp_path: Path):
    """Successes can bring a demoted skill back above threshold."""
    sc = SkillScorer(db_path=tmp_path / "scores.db")
    for _ in range(5):
        sc.record_invocation("rocky")
        sc.record_failure("rocky")
    assert sc.should_suggest("rocky") is False
    for _ in range(15):
        sc.record_invocation("rocky")
        sc.record_success("rocky")
    assert sc.should_suggest("rocky") is True


# ─── Cycle 4: CLI sera skills scores ──────────────────────────────


def test_cli_skills_scores_empty(tmp_path: Path):
    from click.testing import CliRunner
    from sera.cli.main import main

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["skills", "--root", str(tmp_path / "skills"), "scores",
         "--db", str(tmp_path / "scores.db")],
    )
    assert result.exit_code == 0, result.output
    assert "no scores" in result.output.lower() or "0" in result.output


def test_cli_skills_scores_shows_table(tmp_path: Path):
    from click.testing import CliRunner
    from sera.cli.main import main

    sc = SkillScorer(db_path=tmp_path / "scores.db")
    for _ in range(3):
        sc.record_invocation("my_skill")
        sc.record_success("my_skill")

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["skills", "--root", str(tmp_path / "skills"), "scores",
         "--db", str(tmp_path / "scores.db")],
    )
    assert result.exit_code == 0, result.output
    assert "my_skill" in result.output
