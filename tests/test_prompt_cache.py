"""P-06: freeze-at-start system prompt + cache_control on tool blocks."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from sera.llm.cache import (
    ANTHROPIC_TOOL_RESULT_CACHE_WINDOW,
    CacheUsage,
    FrozenPromptMismatch,
    apply_cache_control_anthropic,
    freeze_system_prompt,
    hash_prompt,
    parse_anthropic_usage,
)
from sera.memory.session import Session


def test_freeze_persists_prompt_and_hash(tmp_path: Path) -> None:
    db = tmp_path / "sessions.db"
    s = Session.create(workspace=str(tmp_path), db_path=db)
    prompt = "You are Sera. Be concise."

    frozen = freeze_system_prompt(s, prompt)
    assert frozen == prompt

    row = s.conn.execute(
        "SELECT system_prompt, system_prompt_hash FROM sessions WHERE id = ?",
        (s.id,),
    ).fetchone()
    assert row["system_prompt"] == prompt
    assert row["system_prompt_hash"] == hash_prompt(prompt)


def test_freeze_idempotent_returns_stored_prompt(tmp_path: Path) -> None:
    db = tmp_path / "sessions.db"
    s = Session.create(workspace=str(tmp_path), db_path=db)

    first = freeze_system_prompt(s, "original prompt")
    # Second call with a *different* prompt must still return the original
    # so the Anthropic prompt-cache prefix stays stable for the session.
    second = freeze_system_prompt(s, "TAMPERED prompt")

    assert first == "original prompt"
    assert second == "original prompt"


def test_freeze_survives_reload(tmp_path: Path) -> None:
    db = tmp_path / "sessions.db"
    s = Session.create(workspace=str(tmp_path), db_path=db)
    freeze_system_prompt(s, "frozen forever")
    s.close()

    reloaded = Session.load(s.id, db_path=db)
    assert reloaded is not None
    assert freeze_system_prompt(reloaded, "ignored") == "frozen forever"


def test_freeze_detects_tampering(tmp_path: Path) -> None:
    db = tmp_path / "sessions.db"
    s = Session.create(workspace=str(tmp_path), db_path=db)
    freeze_system_prompt(s, "honest prompt")

    # Direct DB tampering: change the prompt body without updating its hash.
    s.conn.execute(
        "UPDATE sessions SET system_prompt = ? WHERE id = ?",
        ("malicious replacement", s.id),
    )
    s.conn.commit()

    with pytest.raises(FrozenPromptMismatch):
        freeze_system_prompt(s, "honest prompt")


def test_apply_cache_control_tags_system_block() -> None:
    system_blocks, _ = apply_cache_control_anthropic("hello", [])
    assert len(system_blocks) == 1
    assert system_blocks[0]["type"] == "text"
    assert system_blocks[0]["text"] == "hello"
    assert system_blocks[0]["cache_control"] == {"type": "ephemeral"}


def test_apply_cache_control_marks_last_three_tool_results() -> None:
    # Build 5 user turns each containing a single tool_result block.
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": f"t{i}", "content": f"r{i}"}
            ],
        }
        for i in range(5)
    ]

    _, out = apply_cache_control_anthropic("sys", messages)

    marked = [
        i
        for i, m in enumerate(out)
        if isinstance(m.get("content"), list)
        and m["content"][-1].get("cache_control") == {"type": "ephemeral"}
    ]
    assert marked == [2, 3, 4]
    assert ANTHROPIC_TOOL_RESULT_CACHE_WINDOW == 3


def test_apply_cache_control_with_fewer_than_window() -> None:
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "t0", "content": "r0"}
            ],
        }
    ]
    _, out = apply_cache_control_anthropic("sys", messages)
    assert out[0]["content"][-1]["cache_control"] == {"type": "ephemeral"}


def test_apply_cache_control_does_not_mutate_input() -> None:
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "t", "content": "r"}
            ],
        }
    ]
    apply_cache_control_anthropic("sys", messages)
    assert "cache_control" not in messages[0]["content"][0]


def test_apply_cache_control_ignores_plain_user_text() -> None:
    messages = [{"role": "user", "content": "hi"}]
    _, out = apply_cache_control_anthropic("sys", messages)
    # Plain string content untouched; only tool_result lists get markers.
    assert out[0]["content"] == "hi"


def test_parse_anthropic_usage_from_dict() -> None:
    u = parse_anthropic_usage(
        {
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_read_input_tokens": 800,
            "cache_creation_input_tokens": 200,
        }
    )
    assert u.input_tokens == 100
    assert u.cache_read_input_tokens == 800
    assert u.total_input == 1100
    assert u.cache_hit_ratio == pytest.approx(800 / 1100)


def test_parse_anthropic_usage_handles_none() -> None:
    u = parse_anthropic_usage(None)
    assert u == CacheUsage()
    assert u.cache_hit_ratio == 0.0


def test_parse_anthropic_usage_from_object() -> None:
    class _Usage:
        input_tokens = 10
        output_tokens = 5
        cache_read_input_tokens = 20
        cache_creation_input_tokens = 0

    u = parse_anthropic_usage(_Usage())
    assert u.cache_read_input_tokens == 20


def test_record_usage_accumulates(tmp_path: Path) -> None:
    db = tmp_path / "sessions.db"
    s = Session.create(workspace=str(tmp_path), db_path=db)
    s.record_usage(
        input_tokens=10,
        output_tokens=5,
        cache_read_tokens=80,
        cache_creation_tokens=20,
    )
    s.record_usage(
        input_tokens=4,
        output_tokens=2,
        cache_read_tokens=90,
        cache_creation_tokens=0,
    )
    totals = s.usage_totals()
    assert totals == {
        "input_tokens": 14,
        "output_tokens": 7,
        "cache_read_tokens": 170,
        "cache_creation_tokens": 20,
    }


def test_migration_adds_columns_to_legacy_db(tmp_path: Path) -> None:
    """Existing pre-P-06 sessions.db gets columns added on next connect."""
    db = tmp_path / "legacy.db"
    legacy = sqlite3.connect(db)
    legacy.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            title TEXT,
            workspace TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        """
    )
    legacy.execute(
        "INSERT INTO sessions (id, title, workspace, created_at, updated_at) "
        "VALUES ('abc', 't', 'w', 0, 0)"
    )
    legacy.commit()
    legacy.close()

    reloaded = Session.load("abc", db_path=db)
    assert reloaded is not None
    assert freeze_system_prompt(reloaded, "fresh prompt") == "fresh prompt"
    totals = reloaded.usage_totals()
    assert totals["cache_read_tokens"] == 0
