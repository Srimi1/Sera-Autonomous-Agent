"""file_read — read text file under workspace root."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from sera.tools.base import Permission, Tool, ToolContext, ToolScope
from sera.tools.registry import register

MAX_BYTES = 256_000


async def _handler(args: dict[str, Any], ctx: ToolContext) -> str:
    raw_path = args["path"]
    p = (Path(ctx.workspace) / raw_path).resolve()
    workspace = Path(ctx.workspace).resolve()
    if workspace not in p.parents and p != workspace:
        return f"Refused: path escapes workspace ({p})"
    if p.is_symlink():
        real = p.resolve(strict=True)
        if workspace not in real.parents and real != workspace:
            return f"Refused: symlink target escapes workspace ({real})"
    if not p.exists():
        return f"Not found: {raw_path}"
    if not p.is_file():
        return f"Not a file: {raw_path}"
    fd = os.open(str(p), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        data = os.read(fd, MAX_BYTES)
    finally:
        os.close(fd)
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="replace") + "\n[binary content, replaced bad bytes]"


register(
    Tool(
        name="file_read",
        description="Read a UTF-8 text file under the workspace. Up to 256 KB.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path relative to workspace."}
            },
            "required": ["path"],
        },
        permission=Permission.READ_ONLY,
        scope=ToolScope.SYSTEM,
        handler=_handler,
    )
)
