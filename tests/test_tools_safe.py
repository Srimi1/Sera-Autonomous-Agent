"""Safety boundary tests: workspace escape + dangerous shell classifier."""
from __future__ import annotations

import asyncio
from pathlib import Path

from sera.tools.base import Permission, ToolContext
from sera.tools.impl.file_read import _handler as file_read_handler
from sera.tools.impl.shell_run import classify


def test_file_read_blocks_escape(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("nope")
    ctx = ToolContext(session_id="s", workspace=str(ws))
    result = asyncio.run(file_read_handler({"path": "../secret.txt"}, ctx))
    assert "Refused" in result


def test_file_read_inside_ok(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "hello.txt").write_text("hi")
    ctx = ToolContext(session_id="s", workspace=str(ws))
    result = asyncio.run(file_read_handler({"path": "hello.txt"}, ctx))
    assert result == "hi"


def test_shell_classifier():
    assert classify("rm -rf /") == Permission.DANGEROUS
    assert classify("sudo apt update") == Permission.DANGEROUS
    assert classify("dd if=/dev/zero of=/dev/sda") == Permission.DANGEROUS
    assert classify("ls -la") == Permission.EXECUTE
    assert classify("python -V") == Permission.EXECUTE
    assert classify(":(){ :|:& };:") == Permission.DANGEROUS
