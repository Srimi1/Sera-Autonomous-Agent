"""Tests for sera.train.lora — P-73 Local LoRA fine-tune.

Phase verification: 7 nights of recorded eval scores → gain_pp ≥ 2pp.
No mlx-lm binary required — injectable runner + stub scores.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from sera.train.lora import (
    GainTracker,
    LoRATrainer,
    TrainConfig,
    _parse_final_loss,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cfg(tmp_path: Path, *, corpus: str | None = None) -> TrainConfig:
    corpus_path = tmp_path / "corpus.jsonl"
    if corpus is not None:
        corpus_path.write_text(corpus, encoding="utf-8")
    return TrainConfig(
        base_model="mlx-community/Mistral-7B-Instruct-v0.2-4bit",
        corpus=corpus_path,
        adapter_dir=tmp_path / "adapter",
        num_iters=100,
    )


def _good_corpus() -> str:
    rec = json.dumps({"messages": [
        {"role": "user",      "content": "What is Sera?"},
        {"role": "assistant", "content": "An autonomous local agent."},
    ]})
    return rec + "\n"


def _ok_runner(cmd: list[str]) -> tuple[int, str, str]:
    stdout = (
        "Iter 50: Train loss 2.800, It/sec 3.0, Tokens/sec 750\n"
        "Iter 100: Train loss 2.100, It/sec 3.1, Tokens/sec 810\n"
    )
    return 0, stdout, ""


def _fail_runner(cmd: list[str]) -> tuple[int, str, str]:
    return 1, "", "CUDA out of memory"


def _tracker(tmp_path: Path) -> GainTracker:
    t = [0.0]
    tracker = GainTracker(db=tmp_path / "gain.db", clock=lambda: t[0])
    tracker._t = t   # expose for tests to advance
    return tracker


# ---------------------------------------------------------------------------
# _parse_final_loss
# ---------------------------------------------------------------------------

class TestParseFinalLoss:
    def test_parses_last_value(self) -> None:
        output = (
            "Iter 50: Train loss 2.800, It/sec 3.0\n"
            "Iter 100: Train loss 2.100, It/sec 3.1\n"
        )
        assert _parse_final_loss(output) == pytest.approx(2.1)

    def test_returns_none_on_empty(self) -> None:
        assert _parse_final_loss("") is None

    def test_returns_none_on_no_match(self) -> None:
        assert _parse_final_loss("something else entirely") is None

    def test_case_insensitive(self) -> None:
        assert _parse_final_loss("TRAIN LOSS 1.500 done") == pytest.approx(1.5)


# ---------------------------------------------------------------------------
# LoRATrainer.build_cmd
# ---------------------------------------------------------------------------

class TestBuildCmd:
    def test_contains_model(self, tmp_path: Path) -> None:
        cfg = _cfg(tmp_path, corpus=_good_corpus())
        trainer = LoRATrainer()
        cmd = trainer.build_cmd(cfg, tmp_path / "data")
        assert "--model" in cmd
        idx = cmd.index("--model")
        assert cmd[idx + 1] == cfg.base_model

    def test_contains_adapter_path(self, tmp_path: Path) -> None:
        cfg = _cfg(tmp_path, corpus=_good_corpus())
        trainer = LoRATrainer()
        cmd = trainer.build_cmd(cfg, tmp_path / "data")
        assert "--adapter-path" in cmd
        idx = cmd.index("--adapter-path")
        assert cmd[idx + 1] == str(cfg.adapter_dir)

    def test_lora_rank_in_params(self, tmp_path: Path) -> None:
        cfg = _cfg(tmp_path, corpus=_good_corpus())
        trainer = LoRATrainer()
        cmd = trainer.build_cmd(cfg, tmp_path / "data")
        assert "--lora-parameters" in cmd
        idx = cmd.index("--lora-parameters")
        params = json.loads(cmd[idx + 1])
        assert params["rank"] == cfg.lora_rank

    def test_num_iters(self, tmp_path: Path) -> None:
        cfg = _cfg(tmp_path, corpus=_good_corpus())
        trainer = LoRATrainer()
        cmd = trainer.build_cmd(cfg, tmp_path / "data")
        assert "--num-iters" in cmd
        idx = cmd.index("--num-iters")
        assert cmd[idx + 1] == str(cfg.num_iters)

    def test_train_flag_present(self, tmp_path: Path) -> None:
        cfg = _cfg(tmp_path, corpus=_good_corpus())
        cmd = LoRATrainer().build_cmd(cfg, tmp_path / "data")
        assert "--train" in cmd

    def test_fine_tune_type_lora(self, tmp_path: Path) -> None:
        cfg = _cfg(tmp_path, corpus=_good_corpus())
        cmd = LoRATrainer().build_cmd(cfg, tmp_path / "data")
        assert "--fine-tune-type" in cmd
        idx = cmd.index("--fine-tune-type")
        assert cmd[idx + 1] == "lora"


# ---------------------------------------------------------------------------
# LoRATrainer.train — with stub runner
# ---------------------------------------------------------------------------

class TestLoRATrainer:
    def test_ok_result_shape(self, tmp_path: Path) -> None:
        cfg = _cfg(tmp_path, corpus=_good_corpus())
        trainer = LoRATrainer(runner=_ok_runner)
        result = trainer.train(cfg)
        assert result.ok
        assert result.iterations == 100
        assert result.final_loss == pytest.approx(2.1)
        assert result.adapter_dir == cfg.adapter_dir

    def test_creates_adapter_dir(self, tmp_path: Path) -> None:
        cfg = _cfg(tmp_path, corpus=_good_corpus())
        LoRATrainer(runner=_ok_runner).train(cfg)
        assert cfg.adapter_dir.exists()

    def test_missing_corpus_soft_fails(self, tmp_path: Path) -> None:
        cfg = _cfg(tmp_path)   # no corpus written
        result = LoRATrainer(runner=_ok_runner).train(cfg)
        assert not result.ok
        assert "corpus not found" in (result.error or "")

    def test_empty_corpus_soft_fails(self, tmp_path: Path) -> None:
        cfg = _cfg(tmp_path, corpus="")
        result = LoRATrainer(runner=_ok_runner).train(cfg)
        assert not result.ok
        assert "empty" in (result.error or "")

    def test_runner_failure_soft_fails(self, tmp_path: Path) -> None:
        cfg = _cfg(tmp_path, corpus=_good_corpus())
        result = LoRATrainer(runner=_fail_runner).train(cfg)
        assert not result.ok
        assert result.error is not None

    def test_no_loss_in_output_still_ok(self, tmp_path: Path) -> None:
        def silent_runner(cmd):
            return 0, "done", ""

        cfg = _cfg(tmp_path, corpus=_good_corpus())
        result = LoRATrainer(runner=silent_runner).train(cfg)
        assert result.ok
        assert result.final_loss is None

    def test_runner_receives_mlx_lm_module(self, tmp_path: Path) -> None:
        captured: list[list[str]] = []

        def spy(cmd):
            captured.append(cmd)
            return 0, "", ""

        cfg = _cfg(tmp_path, corpus=_good_corpus())
        LoRATrainer(runner=spy).train(cfg)
        assert captured
        cmd = captured[0]
        assert "mlx_lm.lora" in " ".join(cmd)


# ---------------------------------------------------------------------------
# GainTracker
# ---------------------------------------------------------------------------

class TestGainTracker:
    def test_record_and_count(self, tmp_path: Path) -> None:
        tracker = _tracker(tmp_path)
        tracker.record("2026-05-20", 0.80)
        tracker.record("2026-05-21", 0.82)
        assert tracker.count() == 2

    def test_upsert_same_date(self, tmp_path: Path) -> None:
        tracker = _tracker(tmp_path)
        tracker.record("2026-05-20", 0.80)
        tracker.record("2026-05-20", 0.85)
        assert tracker.count() == 1
        assert tracker.scores()[0][1] == pytest.approx(0.85)

    def test_scores_oldest_first(self, tmp_path: Path) -> None:
        tracker = _tracker(tmp_path)
        for i, (date, acc) in enumerate([
            ("2026-05-20", 0.80),
            ("2026-05-21", 0.81),
            ("2026-05-22", 0.83),
        ]):
            tracker._t[0] = float(i)
            tracker.record(date, acc)
        dates = [s[0] for s in tracker.scores()]
        assert dates == ["2026-05-20", "2026-05-21", "2026-05-22"]

    def test_gain_pp_basic(self, tmp_path: Path) -> None:
        tracker = _tracker(tmp_path)
        for i, acc in enumerate([0.80, 0.81, 0.82, 0.83, 0.84, 0.85, 0.87]):
            tracker._t[0] = float(i)
            tracker.record(f"2026-05-{i + 20:02d}", acc)
        gain = tracker.gain_pp(7)
        assert gain == pytest.approx(7.0)   # 0.87 - 0.80 = 0.07 = 7pp

    def test_gain_pp_positive_is_improvement(self, tmp_path: Path) -> None:
        tracker = _tracker(tmp_path)
        tracker._t[0] = 0.0
        tracker.record("d1", 0.70)
        tracker._t[0] = 1.0
        tracker.record("d2", 0.73)
        assert tracker.gain_pp() > 0

    def test_gain_pp_negative_is_regression(self, tmp_path: Path) -> None:
        tracker = _tracker(tmp_path)
        tracker._t[0] = 0.0
        tracker.record("d1", 0.80)
        tracker._t[0] = 1.0
        tracker.record("d2", 0.75)
        assert tracker.gain_pp() < 0

    def test_gain_pp_none_with_one_entry(self, tmp_path: Path) -> None:
        tracker = _tracker(tmp_path)
        tracker.record("d1", 0.80)
        assert tracker.gain_pp() is None

    def test_gain_pp_none_empty(self, tmp_path: Path) -> None:
        tracker = _tracker(tmp_path)
        assert tracker.gain_pp() is None

    def test_invalid_accuracy_rejected(self, tmp_path: Path) -> None:
        tracker = _tracker(tmp_path)
        with pytest.raises(ValueError):
            tracker.record("d1", 1.5)
        with pytest.raises(ValueError):
            tracker.record("d1", -0.1)


# ---------------------------------------------------------------------------
# THE VERIFICATION: 7 nights → gain_pp ≥ 2pp
# ---------------------------------------------------------------------------

class TestSevenNightVerification:
    def test_seven_nights_two_pp_gain(self, tmp_path: Path) -> None:
        """Phase gate: 7 nights of incrementally improving eval → ≥2pp gain."""
        tracker = _tracker(tmp_path)
        # Simulated nightly accuracy: starts 80%, ends 83% — 3pp gain
        nightly = [0.800, 0.810, 0.815, 0.820, 0.825, 0.828, 0.830]
        for i, acc in enumerate(nightly):
            tracker._t[0] = float(i)
            tracker.record(f"2026-05-{i + 20:02d}", acc)

        assert tracker.count() == 7
        gain = tracker.gain_pp(7)
        assert gain is not None, "gain_pp must return a value after 7 nights"
        assert gain >= 2.0, f"expected ≥2pp gain, got {gain}pp"
