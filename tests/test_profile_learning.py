from __future__ import annotations

import asyncio
from pathlib import Path

from click.testing import CliRunner

from sera.cli.main import main
from sera.memory.session import Message, Session
from sera.profile import load_profile_text
from sera.profile_learning import (
    STATUS_APPLIED,
    STATUS_PENDING,
    STATUS_REJECTED,
    ProfileLearner,
)


def test_manual_profile_suggestion_roundtrip(tmp_path: Path):
    learner = ProfileLearner(workspace=tmp_path, db_path=tmp_path / "profile.db")
    suggestion = learner.suggest(
        section_key="workflow",
        item="Start substantial work with a concrete implementation plan.",
        reason="User repeatedly asks for plans first.",
    )
    assert suggestion.status == STATUS_PENDING
    pending = learner.pending()
    assert pending and pending[0].item.startswith("Start substantial work")


def test_apply_profile_suggestion_updates_profile_md(tmp_path: Path):
    learner = ProfileLearner(workspace=tmp_path, db_path=tmp_path / "profile.db")
    suggestion = learner.suggest(
        section_key="vetoes",
        item="Do not add fluff, cheerleading, or motivational filler.",
        reason="User dislikes fluff.",
    )
    applied = learner.apply(suggestion.id)
    assert applied.status == STATUS_APPLIED
    text = load_profile_text(tmp_path)
    assert "Do not add fluff, cheerleading, or motivational filler." in text


def test_reject_profile_suggestion_marks_rejected(tmp_path: Path):
    learner = ProfileLearner(workspace=tmp_path, db_path=tmp_path / "profile.db")
    suggestion = learner.suggest(
        section_key="style",
        item="Prefer concise, direct answers.",
        reason="Observed concise preference.",
    )
    rejected = learner.reject(suggestion.id)
    assert rejected.status == STATUS_REJECTED


def test_capture_session_derives_profile_suggestions(tmp_path: Path, monkeypatch):
    import sera.memory.session as session_mod

    monkeypatch.setattr(session_mod, "_LOCKS_DIR", tmp_path / "locks")
    learner = ProfileLearner(workspace=tmp_path, db_path=tmp_path / "profile.db")
    session = Session.create(workspace=str(tmp_path), db_path=tmp_path / "sessions.db")
    session.append(Message(role="user", content="Be terse and concise."))
    session.append(Message(role="assistant", content="ok"))
    session.append(Message(role="user", content="Give me a plan first and verify it."))

    suggestions = asyncio.run(learner.capture_session(session))
    items = {(s.section_key, s.item) for s in suggestions}
    assert ("style", "Prefer concise, direct answers.") in items
    assert ("workflow", "Start substantial work with a concrete implementation plan.") in items


def test_cli_profile_pending_lists_suggestions(tmp_path: Path):
    db_path = tmp_path / "profile.db"
    learner = ProfileLearner(workspace=tmp_path, db_path=db_path)
    learner.suggest(
        section_key="tooling",
        item="Favor reusable skills over one-off prompt habits.",
        reason="Observed workshop preference.",
    )
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "profile",
            "pending",
            "--workspace",
            str(tmp_path),
            "--db",
            str(db_path),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "reusable skills" in result.output.lower()
