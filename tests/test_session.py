"""SQLite session + FTS5 round-trip."""
from __future__ import annotations

from pathlib import Path

from sera.memory.session import Message, Session


def test_create_append_load_search(tmp_path: Path):
    db = tmp_path / "sessions.db"
    s = Session.create(workspace=str(tmp_path), title="t1", db_path=db)
    s.append(Message(role="user", content="hello sera"))
    s.append(Message(role="assistant", content="hi there friend"))

    reloaded = Session.load(s.id, db_path=db)
    assert reloaded is not None
    assert len(reloaded.messages) == 2
    assert reloaded.messages[0].role == "user"
    assert reloaded.messages[1].content == "hi there friend"

    hits = reloaded.search("friend")
    assert any("friend" in snip for _role, snip in hits)


def test_tool_call_persistence(tmp_path: Path):
    db = tmp_path / "sessions.db"
    s = Session.create(workspace=str(tmp_path), db_path=db)
    s.append(
        Message(
            role="assistant",
            content=None,
            tool_calls=[
                {"id": "c1", "type": "function", "function": {"name": "file_read", "arguments": '{"path": "x.txt"}'}}
            ],
        )
    )
    s.append(Message(role="tool", content="contents…", tool_call_id="c1", name="file_read"))

    reloaded = Session.load(s.id, db_path=db)
    assert reloaded is not None
    assert reloaded.messages[0].tool_calls[0]["id"] == "c1"
    assert reloaded.messages[1].tool_call_id == "c1"
