"""file_write — write text file under workspace root."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from sera.tools.base import Permission, Tool, ToolContext, ToolScope
from sera.tools.registry import register


async def _handler(args: dict[str, Any], ctx: ToolContext) -> str:
    raw_path = args["path"]
    content = args["content"]
    p = (Path(ctx.workspace) / raw_path).resolve()
    workspace = Path(ctx.workspace).resolve()
    if workspace not in p.parents and p != workspace:
        return f"Refused: path escapes workspace ({p})"
    p.parent.mkdir(parents=True, exist_ok=True)
    # Re-check after mkdir in case a symlink was planted
    final = p.parent.resolve() / p.name
    if workspace not in final.parents and final != workspace:
        return f"Refused: resolved path escapes workspace ({final})"
    if p.exists() and p.is_symlink():
        real = p.resolve(strict=True)
        if workspace not in real.parents and real != workspace:
            return f"Refused: symlink target escapes workspace ({real})"
    fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o644)
    try:
        os.write(fd, content.encode("utf-8"))
    finally:
        os.close(fd)
    return f"Wrote {len(content)} chars to {raw_path}"


register(
    Tool(
        name="file_write",
        description="Write UTF-8 text to a file under the workspace. Creates parent dirs.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path relative to workspace."},
                "content": {"type": "string", "description": "File contents."},
            },
            "required": ["path", "content"],
        },
        permission=Permission.WRITE,
        scope=ToolScope.SYSTEM,
        handler=_handler,
    )
)
