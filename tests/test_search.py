"""FTS5 query escaping + current-session scoping."""
from __future__ import annotations

from pathlib import Path

from sera.memory.session import Message, Session, _escape_fts5


def test_escape_handles_specials():
    assert _escape_fts5("foo") == '"foo"'
    assert _escape_fts5('a"b') == '"a""b"'
    assert _escape_fts5("foo:bar AND baz*") == '"foo:bar AND baz*"'
    assert _escape_fts5("") == '""'


def test_search_with_special_chars_does_not_crash(tmp_path: Path):
    db = tmp_path / "sessions.db"
    s = Session.create(workspace=str(tmp_path), db_path=db)
    s.append(Message(role="user", content="ratio is foo:bar"))
    # FTS5 would normally choke on `foo:bar` — escaping must save us.
    hits = s.search("foo:bar")
    assert isinstance(hits, list)


def test_current_only_scopes(tmp_path: Path):
    db = tmp_path / "sessions.db"
    s1 = Session.create(workspace=str(tmp_path), db_path=db)
    s1.append(Message(role="user", content="orange whale"))
    s2 = Session.create(workspace=str(tmp_path), db_path=db)
    s2.append(Message(role="user", content="orange dolphin"))

    cross = s2.search("orange")
    assert len(cross) == 2
    own = s2.search("orange", current_only=True)
    assert len(own) == 1
    assert "dolphin" in own[0][1]
