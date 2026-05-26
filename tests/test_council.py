"""P-31: council module — N=3 parallel models, anonymous labels (TDD)."""
from __future__ import annotations

import asyncio
from collections import Counter


from sera.council.runner import (
    COUNCIL_LABELS,
    CouncilAnswer,
    CouncilRun,
    run_council,
)


# ─── Helpers ──────────────────────────────────────────────────────


def _make_llm_factory(responses: dict[str, str]):
    """Factory that returns a stub async callable per model_id."""
    def factory(model_id: str):
        async def call(prompt: str) -> str:
            return responses.get(model_id, f"answer from {model_id}")
        return call
    return factory


_THREE_MODELS = ["model-alpha", "model-beta", "model-gamma"]
_DEFAULT_FACTORY = _make_llm_factory({
    "model-alpha": "Paris is the capital of France.",
    "model-beta":  "The capital of France is Paris.",
    "model-gamma": "France's capital: Paris.",
})


# ─── Cycle 1: run_council returns 3 answers in parallel ───────────


def test_run_council_returns_three_answers():
    run = asyncio.run(run_council("Capital of France?", _THREE_MODELS, _DEFAULT_FACTORY))
    assert isinstance(run, CouncilRun)
    assert len(run.answers) == 3


def test_run_council_answers_are_council_answer_instances():
    run = asyncio.run(run_council("Q?", _THREE_MODELS, _DEFAULT_FACTORY))
    for a in run.answers:
        assert isinstance(a, CouncilAnswer)


def test_run_council_all_labels_present():
    run = asyncio.run(run_council("Q?", _THREE_MODELS, _DEFAULT_FACTORY))
    labels = {a.label for a in run.answers}
    assert labels == set(COUNCIL_LABELS[:3])


def test_run_council_answers_have_content():
    run = asyncio.run(run_council("Q?", _THREE_MODELS, _DEFAULT_FACTORY))
    for a in run.answers:
        assert a.content


def test_run_council_ran_at_set():
    run = asyncio.run(run_council("Q?", _THREE_MODELS, _DEFAULT_FACTORY))
    assert run.ran_at > 0.0


# ─── Cycle 2: label randomisation + model isolation ───────────────


def test_labels_are_randomised_across_runs():
    """Same models should NOT always get the same label."""
    label_for_alpha: list[str] = []
    for _ in range(20):
        run = asyncio.run(run_council("Q?", _THREE_MODELS, _DEFAULT_FACTORY))
        label_map = run.label_map
        label_for_alpha.append(label_map["model-alpha"])
    # With random shuffle, label distribution should not be all identical.
    counts = Counter(label_for_alpha)
    assert len(counts) > 1, "label assignment never varied — shuffle is broken"


def test_label_map_covers_all_models():
    run = asyncio.run(run_council("Q?", _THREE_MODELS, _DEFAULT_FACTORY))
    assert set(run.label_map.keys()) == set(_THREE_MODELS)


def test_answer_content_does_not_contain_model_id():
    """Model IDs must not appear in the answer content (no self-identification)."""
    factory = _make_llm_factory({
        "model-alpha": "The answer is 42.",
        "model-beta":  "42 is the answer.",
        "model-gamma": "Answer: 42.",
    })
    run = asyncio.run(run_council("Q?", _THREE_MODELS, factory))
    for a in run.answers:
        for model_id in _THREE_MODELS:
            assert model_id not in a.content, \
                f"model_id {model_id!r} leaked into answer for label {a.label}"


def test_answer_label_not_in_label_map_values_for_other_models():
    """The label_map maps model→label. Answers only expose labels."""
    run = asyncio.run(run_council("Q?", _THREE_MODELS, _DEFAULT_FACTORY))
    for a in run.answers:
        # Each answer's label must exist in the label_map values.
        assert a.label in run.label_map.values()
        # The model_id for this label should be resolvable.
        model_for_label = {v: k for k, v in run.label_map.items()}[a.label]
        assert model_for_label in _THREE_MODELS


def test_two_model_council():
    models = ["m1", "m2"]
    factory = _make_llm_factory({"m1": "yes", "m2": "no"})
    run = asyncio.run(run_council("Q?", models, factory))
    assert len(run.answers) == 2
    assert {a.label for a in run.answers} == set(COUNCIL_LABELS[:2])


# ─── Cycle 3: partial failure tolerance ───────────────────────────


def test_single_model_failure_returns_partial_run():
    """One model crashing must not crash the whole council."""
    def failing_factory(model_id: str):
        async def call(prompt: str) -> str:
            if model_id == "model-beta":
                raise RuntimeError("LLM down")
            return f"answer from {model_id}"
        return call

    run = asyncio.run(run_council("Q?", _THREE_MODELS, failing_factory))
    # At least 2 answers should succeed.
    successful = [a for a in run.answers if a.error is None]
    failed = [a for a in run.answers if a.error is not None]
    assert len(successful) == 2
    assert len(failed) == 1
    assert "LLM down" in failed[0].error


def test_all_models_fail_returns_empty_successful_answers():
    def all_fail(model_id: str):
        async def call(prompt: str) -> str:
            raise RuntimeError("all down")
        return call

    run = asyncio.run(run_council("Q?", _THREE_MODELS, all_fail))
    assert all(a.error is not None for a in run.answers)


def test_latency_ms_recorded():
    run = asyncio.run(run_council("Q?", _THREE_MODELS, _DEFAULT_FACTORY))
    for a in run.answers:
        if a.error is None:
            assert a.latency_ms >= 0.0


# ─── Cycle 4: CLI sera council run ────────────────────────────────


def test_cli_council_run_dry(tmp_path):
    """CLI smoke test — dry-run with stub factory prints labels."""
    from click.testing import CliRunner
    from sera.cli.main import main

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["council", "run", "What is 2+2?", "--dry-run"],
    )
    assert result.exit_code == 0, result.output
    # Dry-run should show the labels A, B, C (or subset).
    assert any(label in result.output for label in COUNCIL_LABELS)


def test_cli_council_run_shows_answers(tmp_path):
    from click.testing import CliRunner
    from sera.cli.main import main

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["council", "run", "Capital of France?", "--dry-run"],
    )
    assert result.exit_code == 0, result.output
    assert "A" in result.output or "B" in result.output
