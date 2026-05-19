"""Tool auto-discovery."""
from __future__ import annotations

from sera.tools.registry import all_tools, get, reset


def test_starter_tools_register():
    reset()
    names = {t.name for t in all_tools()}
    assert {"file_read", "file_write", "shell_run", "web_search", "memory_store"} <= names


def test_schema_shapes():
    reset()
    fr = get("file_read")
    assert fr is not None
    oai = fr.to_openai_schema()
    assert oai["type"] == "function"
    assert oai["function"]["name"] == "file_read"
    anth = fr.to_anthropic_schema()
    assert anth["name"] == "file_read"
    assert "input_schema" in anth
