"""Tests for sera.llm.bandit — Thompson sampling router."""
from __future__ import annotations

import random
from pathlib import Path

import pytest

from sera.llm.bandit import ThompsonBandit, reward_signal


PROFILES = ["cheap", "big"]


@pytest.fixture()
def bandit() -> ThompsonBandit:
    return ThompsonBandit(rng=random.Random(42))


# ---------------------------------------------------------------------------
# reward_signal
# ---------------------------------------------------------------------------

class TestRewardSignal:
    def test_failure_always_zero(self) -> None:
        assert reward_signal(success=False, latency_ms=100, cost_usd=0.0) == 0.0

    def test_success_within_budget(self) -> None:
        assert reward_signal(success=True, latency_ms=500, cost_usd=0.001) == 1.0

    def test_latency_exceeded(self) -> None:
        assert reward_signal(success=True, latency_ms=15_000, cost_usd=0.001) == 0.0

    def test_cost_exceeded(self) -> None:
        assert reward_signal(success=True, latency_ms=500, cost_usd=0.5) == 0.0

    def test_exact_boundary(self) -> None:
        assert reward_signal(success=True, latency_ms=10_000, cost_usd=0.005) == 1.0

    def test_custom_budgets(self) -> None:
        assert reward_signal(
            success=True, latency_ms=500, cost_usd=0.1,
            latency_budget_ms=1_000, cost_budget_usd=0.2,
        ) == 1.0


# ---------------------------------------------------------------------------
# ThompsonBandit — construction and arms
# ---------------------------------------------------------------------------

class TestBanditInit:
    def test_uniform_prior(self, bandit: ThompsonBandit) -> None:
        # Before any update, mean should be 0.5 (Beta(1,1))
        assert bandit.mean("cheap", "chat") == pytest.approx(0.5)

    def test_single_profile_always_picked(self, bandit: ThompsonBandit) -> None:
        for _ in range(20):
            assert bandit.pick("chat", ["only"]) == "only"

    def test_empty_profiles_raises(self, bandit: ThompsonBandit) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            bandit.pick("chat", [])


# ---------------------------------------------------------------------------
# ThompsonBandit — update
# ---------------------------------------------------------------------------

class TestBanditUpdate:
    def test_reward_1_increases_mean(self, bandit: ThompsonBandit) -> None:
        for _ in range(20):
            bandit.update("cheap", "chat", reward=1.0)
        assert bandit.mean("cheap", "chat") > 0.9

    def test_reward_0_decreases_mean(self, bandit: ThompsonBandit) -> None:
        for _ in range(20):
            bandit.update("cheap", "chat", reward=0.0)
        assert bandit.mean("cheap", "chat") < 0.1

    def test_invalid_reward_raises(self, bandit: ThompsonBandit) -> None:
        with pytest.raises(ValueError, match="reward must be in"):
            bandit.update("cheap", "chat", reward=1.5)

    def test_task_kinds_independent(self, bandit: ThompsonBandit) -> None:
        bandit.update("cheap", "summarize", reward=1.0)
        bandit.update("cheap", "summarize", reward=1.0)
        # plan arm untouched — still uniform
        assert bandit.mean("cheap", "plan") == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# ThompsonBandit — convergence (P-37 verification criterion)
# ---------------------------------------------------------------------------

class TestConvergence:
    """200 synthetic turns: cheap wins summarize, big wins plan."""

    @staticmethod
    def _train(bandit: ThompsonBandit, n_each: int = 50) -> None:
        """Simulate n_each turns per (profile, task_kind) with binary rewards.

        summarize: cheap=1, big=0  — cheap task, cheap model wins
        plan:      cheap=0, big=1  — hard task, big model wins
        Total turns = 4 × n_each = 200 when n_each=50.
        """
        for _ in range(n_each):
            bandit.update("cheap", "summarize", reward=1.0)
            bandit.update("big",   "summarize", reward=0.0)
            bandit.update("cheap", "plan",      reward=0.0)
            bandit.update("big",   "plan",      reward=1.0)

    def test_cheap_wins_summarize(self) -> None:
        b = ThompsonBandit(rng=random.Random(0))
        self._train(b, n_each=50)  # 200 total turns
        # Run 100 picks — cheap must win the overwhelming majority
        wins = sum(1 for _ in range(100) if b.pick("summarize", PROFILES) == "cheap")
        assert wins >= 95, f"cheap won only {wins}/100 picks for summarize"

    def test_big_wins_plan(self) -> None:
        b = ThompsonBandit(rng=random.Random(0))
        self._train(b, n_each=50)
        wins = sum(1 for _ in range(100) if b.pick("plan", PROFILES) == "big")
        assert wins >= 95, f"big won only {wins}/100 picks for plan"

    def test_best_profile_deterministic(self) -> None:
        b = ThompsonBandit(rng=random.Random(0))
        self._train(b, n_each=50)
        assert b.best_profile("summarize", PROFILES) == "cheap"
        assert b.best_profile("plan", PROFILES) == "big"

    def test_mean_ordering_after_training(self) -> None:
        b = ThompsonBandit(rng=random.Random(0))
        self._train(b, n_each=50)
        assert b.mean("cheap", "summarize") > b.mean("big", "summarize")
        assert b.mean("big", "plan") > b.mean("cheap", "plan")


# ---------------------------------------------------------------------------
# ThompsonBandit — seed_from_stats
# ---------------------------------------------------------------------------

class TestSeedFromStats:
    def test_seed_loads_rows(self, tmp_path: Path) -> None:
        from sera.llm.router_stats import record_call

        db = tmp_path / "router_stats.db"
        for _ in range(5):
            record_call(
                provider="openai",
                model="gpt-4o-mini",
                task_kind="chat",
                latency_ms=300,
                input_tokens=100,
                output_tokens=50,
                success=True,
                _db=db,
            )

        b = ThompsonBandit()
        loaded = b.seed_from_stats({"cheap": "gpt-4o-mini"}, _db=db)
        assert loaded == 1  # one (model, task_kind) group
        # alpha should be > 1 (seeded with successes)
        assert b.mean("cheap", "chat") > 0.5

    def test_seed_unknown_model_ignored(self, tmp_path: Path) -> None:
        from sera.llm.router_stats import record_call

        db = tmp_path / "rs.db"
        record_call(
            provider="openai", model="gpt-4-turbo", task_kind="chat",
            latency_ms=800, input_tokens=200, output_tokens=100, success=True, _db=db,
        )
        b = ThompsonBandit()
        loaded = b.seed_from_stats({"cheap": "gpt-4o-mini"}, _db=db)
        assert loaded == 0

    def test_seed_empty_db(self, tmp_path: Path) -> None:
        b = ThompsonBandit()
        loaded = b.seed_from_stats({"cheap": "gpt-4o-mini"}, _db=tmp_path / "missing.db")
        assert loaded == 0


# ---------------------------------------------------------------------------
# ThompsonBandit — state / introspection
# ---------------------------------------------------------------------------

class TestState:
    def test_state_empty(self, bandit: ThompsonBandit) -> None:
        bandit.update("cheap", "chat", reward=0.5)  # touch one arm (reward=0.5 keeps prior mean)
        s = bandit.state()
        assert "cheap/chat" in s
        assert "cheap/chat" in s

    def test_state_after_updates(self, bandit: ThompsonBandit) -> None:
        bandit.update("cheap", "chat", reward=1.0)
        bandit.update("cheap", "chat", reward=1.0)
        s = bandit.state()
        assert s["cheap/chat"]["n"] == 2
        assert s["cheap/chat"]["alpha"] == pytest.approx(3.0)
        assert s["cheap/chat"]["beta"] == pytest.approx(1.0)
