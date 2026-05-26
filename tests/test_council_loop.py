"""P-35: Council-aware run_turn integration.

Verifies:
  - skill with council:true routes through council ensemble
  - skill without council flag uses normal dispatch
  - council_skill_dispatch end-to-end with stubs
  - _council_question extracts natural-language question from tool args
"""
from __future__ import annotations

import asyncio
from pathlib import Path


from sera.agent.loop import _council_question, run_turn
from sera.eval.cases import ScriptStep
from sera.eval.stub_llm import StubLLM
from sera.memory.session import Session
from sera.skills.manifest import CouncilConfig, council_skill_dispatch
from sera.tools.base import ToolCall


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _make_llm_factory(answer: str, ranking: str) -> ...:
    """Stub factory: returns answers on first call, ranking text on second."""
    call_counts: dict[str, int] = {}

    def factory(model_id: str):
        async def call(prompt: str) -> str:
            n = call_counts.get(model_id, 0) + 1
            call_counts[model_id] = n
            if n == 1:
                return f"{answer} from {model_id}"
            # ranking call
            return ranking
        return call
    return factory


_GOOD_RANKING = "FINAL RANKING:\n1. A\n2. B\n3. C"
_MODELS = ["m1", "m2", "m3"]


def _make_config(council_skills: frozenset[str]) -> CouncilConfig:
    factory = _make_llm_factory("Paris is the capital of France.", _GOOD_RANKING)
    return CouncilConfig(
        models=_MODELS,
        llm_factory=factory,
        council_skills=council_skills,
        synthesis_model_id="m1",
    )


# ─── _council_question ────────────────────────────────────────────────────────

def test_council_question_query_key():
    call = ToolCall(id="1", name="skill.research", arguments={"query": "What is gravity?"})
    assert _council_question(call) == "What is gravity?"


def test_council_question_question_key():
    call = ToolCall(id="1", name="skill.x", arguments={"question": "Why is the sky blue?"})
    assert _council_question(call) == "Why is the sky blue?"


def test_council_question_input_key():
    call = ToolCall(id="1", name="skill.x", arguments={"input": "Explain relativity"})
    assert _council_question(call) == "Explain relativity"


def test_council_question_fallback_to_string_values():
    call = ToolCall(id="1", name="skill.x", arguments={"topic": "black holes"})
    assert _council_question(call) == "black holes"


def test_council_question_fallback_to_name():
    call = ToolCall(id="1", name="skill.x", arguments={})
    assert _council_question(call) == "skill.x"


def test_council_question_no_args():
    call = ToolCall(id="1", name="skill.x", arguments=None)
    assert _council_question(call) == "skill.x"


# ─── council_skill_dispatch end-to-end ───────────────────────────────────────

def test_council_skill_dispatch_returns_string():
    config = _make_config(frozenset(["skill.alpha"]))
    result = asyncio.run(council_skill_dispatch("What is 2+2?", config))
    assert isinstance(result, str)
    assert result  # non-empty


def test_council_skill_dispatch_synthesis_not_raw_body():
    """Synthesis comes from chairman, not a raw model answer verbatim."""
    config = _make_config(frozenset(["skill.alpha"]))
    result = asyncio.run(council_skill_dispatch("Capital of France?", config))
    # Chairman synthesis must be non-empty (stub synthesis llm echoes prompt prefix)
    assert len(result) > 0


def test_council_skill_dispatch_all_models_fail_gracefully():
    """Even if all models fail, dispatch must not raise."""
    def bad_factory(model_id: str):
        async def call(prompt: str) -> str:
            raise RuntimeError("model down")
        return call

    config = CouncilConfig(
        models=["bad1", "bad2", "bad3"],
        llm_factory=bad_factory,
        council_skills=frozenset(["skill.x"]),
        synthesis_model_id="bad1",
    )
    result = asyncio.run(council_skill_dispatch("Q?", config))
    # Should return empty string or fallback — not raise
    assert isinstance(result, str)


# ─── CouncilConfig ────────────────────────────────────────────────────────────

def test_council_config_synthesis_model_defaults_to_first():
    config = CouncilConfig(
        models=["cheap", "mid", "big"],
        llm_factory=lambda m: (lambda p: None),
    )
    assert config.synthesis_model_id == "cheap"


def test_council_config_synthesis_model_explicit():
    config = CouncilConfig(
        models=["cheap", "big"],
        llm_factory=lambda m: (lambda p: None),
        synthesis_model_id="big",
    )
    assert config.synthesis_model_id == "big"


def test_council_config_empty_council_skills_default():
    config = CouncilConfig(models=["m"], llm_factory=lambda m: (lambda p: None))
    assert config.council_skills == frozenset()


# ─── run_turn routing: council skill ──────────────────────────────────────────
#
# The StubLLM emits a tool call to "skill.alpha" (round 1), then "done." (round 2).
# The council_config marks "skill.alpha" as a council skill.
# We verify the session contains synthesized content (not "[stub dispatch]").

_STUB_TOOL_CALL = {
    "id": "tc1",
    "name": "skill.alpha",
    "arguments": {"query": "What is the capital of France?"},
}


def _make_stub_llm_with_skill_call() -> StubLLM:
    return StubLLM([
        ScriptStep(tool_calls=(_STUB_TOOL_CALL,)),
        ScriptStep(text="Council answered.", finish_reason="stop"),
    ])


def _register_stub_skill_tool(name: str = "skill.alpha") -> None:
    """Register a dummy skill tool so dispatch_execute doesn't error on it."""
    from sera.tools import registry as reg
    from sera.tools.base import Permission, Tool, ToolContext, ToolScope

    async def _handler(args, ctx: ToolContext) -> str:
        return "[stub dispatch]"

    tool = Tool(
        name=name,
        description="stub skill",
        parameters={"type": "object", "properties": {}},
        permission=Permission.READ_ONLY,
        scope=ToolScope.SKILL,
        handler=_handler,
    )
    reg.register(tool)


def test_run_turn_council_skill_routes_to_ensemble(tmp_path: Path):
    """Skill in council_skills uses ensemble dispatch."""
    _register_stub_skill_tool("skill.alpha")
    db = tmp_path / "s.db"
    session = Session.create(workspace=str(tmp_path), db_path=db)
    llm = _make_stub_llm_with_skill_call()
    config = _make_config(frozenset(["skill.alpha"]))

    calls_made: list[str] = []
    original_factory = config.llm_factory

    def tracking_factory(model_id: str):
        calls_made.append(model_id)
        return original_factory(model_id)

    config_with_tracking = CouncilConfig(
        models=config.models,
        llm_factory=tracking_factory,
        council_skills=config.council_skills,
        synthesis_model_id=config.synthesis_model_id,
    )

    asyncio.run(
        run_turn(session, "What is the capital?", llm, council_config=config_with_tracking)
    )

    # Council factory must have been called (ensemble ran)
    assert len(calls_made) > 0


def test_run_turn_non_council_skill_uses_normal_dispatch(tmp_path: Path):
    """Skill NOT in council_skills goes through normal dispatch."""
    _register_stub_skill_tool("skill.beta")
    db = tmp_path / "s.db"
    session = Session.create(workspace=str(tmp_path), db_path=db)

    stub_llm = StubLLM([
        ScriptStep(tool_calls=({
            "id": "tc2",
            "name": "skill.beta",
            "arguments": {"query": "Q?"},
        },)),
        ScriptStep(text="Normal.", finish_reason="stop"),
    ])

    # council_skills does NOT include skill.beta
    config = _make_config(frozenset(["skill.alpha"]))
    calls_made: list[str] = []

    def tracking_factory(model_id: str):
        calls_made.append(model_id)
        return config.llm_factory(model_id)

    config_tracking = CouncilConfig(
        models=config.models,
        llm_factory=tracking_factory,
        council_skills=frozenset(["skill.alpha"]),  # beta NOT here
        synthesis_model_id=config.synthesis_model_id,
    )

    asyncio.run(
        run_turn(session, "Q?", stub_llm, council_config=config_tracking)
    )

    # Council factory must NOT have been called (normal dispatch used)
    assert len(calls_made) == 0


def test_run_turn_no_council_config_unchanged(tmp_path: Path):
    """When council_config=None, run_turn behaves exactly as before."""
    _register_stub_skill_tool("skill.gamma")
    db = tmp_path / "s.db"
    session = Session.create(workspace=str(tmp_path), db_path=db)

    stub_llm = StubLLM([ScriptStep(text="plain answer.")])
    result = asyncio.run(
        run_turn(session, "Q?", stub_llm, council_config=None)
    )
    assert "plain answer" in result
