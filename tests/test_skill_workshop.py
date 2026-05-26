from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from sera.memory.session import Message
from sera.skills.lifecycle import SkillLifecycle
from sera.skills.loader import reset_default_registries
from sera.skills.workshop import (
    STATUS_PENDING,
    STATUS_QUARANTINED,
    SkillWorkshop,
)
from sera.tools.registry import get as get_tool


def test_explicit_suggest_queues_pending(tmp_path: Path):
    workshop = SkillWorkshop(workspace=tmp_path, db_path=tmp_path / "workshop.db")
    proposal = workshop.suggest(
        skill_name="gif workflow",
        title="Verify GIF assets",
        reason="This workflow recurs.",
        description="Reusable GIF checklist.",
        body="# Verify GIF assets\n\n- verify animation\n- record attribution\n",
    )
    assert proposal.status == STATUS_PENDING
    assert proposal.skill_name == "gif-workflow"
    assert workshop.pending()


def test_risky_suggest_is_quarantined(tmp_path: Path):
    workshop = SkillWorkshop(workspace=tmp_path, db_path=tmp_path / "workshop.db")
    proposal = workshop.suggest(
        skill_name="danger",
        title="Danger",
        reason="Contains unsafe steps.",
        description="Unsafe.",
        body="# Danger\n\n- run sudo rm -rf /tmp/cache\n",
    )
    assert proposal.status == STATUS_QUARANTINED


def test_apply_writes_skill_verifies_and_refreshes_registry(tmp_path: Path):
    reset_default_registries()
    workshop = SkillWorkshop(workspace=tmp_path, db_path=tmp_path / "workshop.db")
    workshop.lifecycle = SkillLifecycle(db_path=tmp_path / "lifecycle.db")
    proposal = workshop.suggest(
        skill_name="animated gif workflow",
        title="Animated GIF Workflow",
        reason="Reusable media QA flow.",
        description="Check animated GIFs before use.",
        body=(
            "# Animated GIF Workflow\n\n"
            "Use this workflow when the same request comes back.\n\n"
            "- verify the URL resolves to an animated GIF\n"
            "- record attribution\n"
        ),
    )
    applied = asyncio.run(workshop.apply(proposal.id))
    assert applied.verified is True
    assert applied.scaffold.skill_path.exists()
    assert applied.scaffold.replay_path.exists()
    assert get_tool("skill.animated-gif-workflow") is not None
    reset_default_registries()


def test_capture_session_turns_durable_correction_into_proposal(tmp_path: Path):
    workshop = SkillWorkshop(workspace=tmp_path, db_path=tmp_path / "workshop.db")
    session = SimpleNamespace(
        id="s1",
        messages=[
            Message(
                role="user",
                content=(
                    "Next time when asked for animated GIFs, verify the URL "
                    "really resolves to an animated GIF and record attribution."
                ),
            ),
            Message(
                role="assistant",
                content="Will do.",
                tool_calls=[{"function": {"name": "web_search", "arguments": "{}"}}],
            ),
        ],
    )
    proposal = asyncio.run(workshop.capture_session(session))
    assert proposal is not None
    assert proposal.skill_name == "animated-gif-workflow"
    assert "record attribution" in proposal.body.lower()
