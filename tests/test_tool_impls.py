"""Backfill coverage for tool implementations the audit flagged as untested:
file_read, file_write, memory_store, python_eval, web_search.

These hit the handlers directly with a ToolContext rather than going through
the dispatcher, so each impl's own logic (path guards, formatting, sandbox
selection, network-failure handling) is exercised in isolation.
"""
from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from sera.tools.base import ToolContext
# Import MODULE OBJECTS (not bare handlers). registry.reset() — called by other
# tests — deletes sera.tools.impl.* from sys.modules; binding handlers and
# patching globals on the SAME module object keeps them consistent regardless
# of reload order. (Patching a string path re-imports a fresh, different object.)
from sera.tools.impl import file_read as file_read_mod
from sera.tools.impl import file_write as file_write_mod
from sera.tools.impl import memory_store as memory_store_mod
from sera.tools.impl import python_eval as python_eval_mod
from sera.tools.impl import web_search as web_search_mod

file_read = file_read_mod._handler
file_write = file_write_mod._handler
memory_store = memory_store_mod._handler
python_eval = python_eval_mod._handler
web_search = web_search_mod._handler


def _ctx(workspace: Path) -> ToolContext:
    return ToolContext(session_id="s1", workspace=str(workspace))


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# file_read
# ---------------------------------------------------------------------------

class TestFileRead:
    def test_reads_utf8(self, tmp_path: Path) -> None:
        (tmp_path / "a.txt").write_text("hello world", encoding="utf-8")
        out = _run(file_read({"path": "a.txt"}, _ctx(tmp_path)))
        assert out == "hello world"

    def test_missing_file(self, tmp_path: Path) -> None:
        out = _run(file_read({"path": "nope.txt"}, _ctx(tmp_path)))
        assert "Not found" in out

    def test_directory_is_not_a_file(self, tmp_path: Path) -> None:
        (tmp_path / "sub").mkdir()
        out = _run(file_read({"path": "sub"}, _ctx(tmp_path)))
        assert "Not a file" in out

    def test_path_escape_refused(self, tmp_path: Path) -> None:
        out = _run(file_read({"path": "../../etc/passwd"}, _ctx(tmp_path)))
        assert "Refused" in out

    def test_truncates_at_max_bytes(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(file_read_mod, "MAX_BYTES", 10)
        (tmp_path / "big.txt").write_text("0123456789ABCDEFG", encoding="utf-8")
        out = _run(file_read({"path": "big.txt"}, _ctx(tmp_path)))
        assert out == "0123456789"

    def test_binary_bytes_replaced(self, tmp_path: Path) -> None:
        (tmp_path / "b.bin").write_bytes(b"\xff\xfe\x00ok")
        out = _run(file_read({"path": "b.bin"}, _ctx(tmp_path)))
        assert "binary content" in out


# ---------------------------------------------------------------------------
# file_write
# ---------------------------------------------------------------------------

class TestFileWrite:
    def test_writes_file(self, tmp_path: Path) -> None:
        out = _run(file_write({"path": "out.txt", "content": "data"}, _ctx(tmp_path)))
        assert "Wrote 4 chars" in out
        assert (tmp_path / "out.txt").read_text() == "data"

    def test_creates_parent_dirs(self, tmp_path: Path) -> None:
        _run(file_write({"path": "deep/nested/f.txt", "content": "x"}, _ctx(tmp_path)))
        assert (tmp_path / "deep" / "nested" / "f.txt").exists()

    def test_path_escape_refused(self, tmp_path: Path) -> None:
        out = _run(file_write({"path": "../evil.txt", "content": "x"}, _ctx(tmp_path)))
        assert "Refused" in out
        assert not (tmp_path.parent / "evil.txt").exists()

    def test_overwrites_existing(self, tmp_path: Path) -> None:
        (tmp_path / "f.txt").write_text("old")
        _run(file_write({"path": "f.txt", "content": "new"}, _ctx(tmp_path)))
        assert (tmp_path / "f.txt").read_text() == "new"


# ---------------------------------------------------------------------------
# memory_store
# ---------------------------------------------------------------------------

class TestMemoryStore:
    @pytest.fixture
    def isolated_db(self, tmp_path: Path, monkeypatch):
        db = tmp_path / "memory.db"
        monkeypatch.setattr(memory_store_mod, "MEMORY_DB", db)
        monkeypatch.setattr(memory_store_mod, "ensure_home", lambda: None)
        return db

    def test_stores_note(self, isolated_db: Path, tmp_path: Path) -> None:
        out = _run(memory_store({"content": "user likes tea"}, _ctx(tmp_path)))
        assert out.startswith("Stored memory #")
        rows = sqlite3.connect(isolated_db).execute(
            "SELECT content, session_id FROM notes"
        ).fetchall()
        assert rows == [("user likes tea", "s1")]

    def test_stores_tags(self, isolated_db: Path, tmp_path: Path) -> None:
        _run(memory_store({"content": "fact", "tags": ["a", "b"]}, _ctx(tmp_path)))
        tags = sqlite3.connect(isolated_db).execute("SELECT tags FROM notes").fetchone()[0]
        assert tags == "a,b"

    def test_fts_searchable(self, isolated_db: Path, tmp_path: Path) -> None:
        _run(memory_store({"content": "the quick brown fox"}, _ctx(tmp_path)))
        hit = sqlite3.connect(isolated_db).execute(
            "SELECT content FROM notes_fts WHERE notes_fts MATCH 'brown'"
        ).fetchone()
        assert hit is not None and "fox" in hit[0]

    def test_increments_ids(self, isolated_db: Path, tmp_path: Path) -> None:
        first = _run(memory_store({"content": "one"}, _ctx(tmp_path)))
        second = _run(memory_store({"content": "two"}, _ctx(tmp_path)))
        assert first != second


# ---------------------------------------------------------------------------
# python_eval (LOCAL subprocess sandbox)
# ---------------------------------------------------------------------------

class TestPythonEval:
    def test_empty_code_required(self, tmp_path: Path) -> None:
        out = _run(python_eval({"code": "   "}, _ctx(tmp_path)))
        assert "code is required" in out

    def test_runs_local_code(self, tmp_path: Path) -> None:
        out = _run(python_eval({"code": "print('hi from sandbox')"}, _ctx(tmp_path)))
        assert "hi from sandbox" in out

    def test_picks_local_for_zero_ceiling(self, tmp_path: Path, monkeypatch) -> None:
        captured = {}

        class _FakeResult:
            def as_tool_output(self) -> str:
                return "ok"

        class _FakeSandbox:
            async def run(self, code, *, timeout, allow_network):
                captured["timeout"] = timeout
                captured["allow_network"] = allow_network
                return _FakeResult()

        def _fake_pick(*, cost_ceiling_usd, require_network):
            captured["ceiling"] = cost_ceiling_usd
            captured["require_network"] = require_network
            return _FakeSandbox()

        monkeypatch.setattr(python_eval_mod, "pick_sandbox", _fake_pick)
        out = _run(python_eval(
            {"code": "x=1", "timeout": 3, "allow_network": True, "cost_ceiling_usd": 0.0},
            _ctx(tmp_path),
        ))
        assert out == "ok"
        assert captured["timeout"] == 3.0
        assert captured["allow_network"] is True
        assert captured["ceiling"] == 0.0


# ---------------------------------------------------------------------------
# web_search (network mocked — never hits DuckDuckGo)
# ---------------------------------------------------------------------------

class TestWebSearch:
    def test_formats_results(self, tmp_path: Path, monkeypatch) -> None:
        def _fake(query, max_results):
            return [
                {"title": "First", "href": "http://a", "body": "body a"},
                {"title": "Second", "url": "http://b", "body": "body b"},
            ]
        monkeypatch.setattr(web_search_mod, "_search_sync", _fake)
        out = _run(web_search({"query": "anything"}, _ctx(tmp_path)))
        assert "1. First" in out
        assert "http://a" in out
        assert "2. Second" in out
        assert "http://b" in out

    def test_no_results(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(web_search_mod, "_search_sync", lambda q, n: [])
        out = _run(web_search({"query": "obscure"}, _ctx(tmp_path)))
        assert "No results" in out

    def test_missing_package_message(self, tmp_path: Path, monkeypatch) -> None:
        def _raise(q, n):
            raise ImportError("no ddgs")
        monkeypatch.setattr(web_search_mod, "_search_sync", _raise)
        out = _run(web_search({"query": "x"}, _ctx(tmp_path)))
        assert "ddgs package not installed" in out

    def test_generic_failure_surfaced(self, tmp_path: Path, monkeypatch) -> None:
        def _raise(q, n):
            raise RuntimeError("rate limited")
        monkeypatch.setattr(web_search_mod, "_search_sync", _raise)
        out = _run(web_search({"query": "x"}, _ctx(tmp_path)))
        assert "web_search failed" in out
        assert "rate limited" in out

    def test_respects_max_results_arg(self, tmp_path: Path, monkeypatch) -> None:
        seen = {}
        def _fake(query, max_results):
            seen["n"] = max_results
            return []
        monkeypatch.setattr(web_search_mod, "_search_sync", _fake)
        _run(web_search({"query": "x", "max_results": 3}, _ctx(tmp_path)))
        assert seen["n"] == 3
