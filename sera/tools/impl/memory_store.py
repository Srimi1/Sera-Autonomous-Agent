"""memory_store — pin a fact to long-term memory. Week-1 stub: writes to memory.db notes table."""
from __future__ import annotations

import sqlite3
import time
from typing import Any

from sera.config import MEMORY_DB, ensure_home
from sera.tools.base import Permission, Tool, ToolContext, ToolScope
from sera.tools.registry import register

_INIT_SQL = """
CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    content TEXT NOT NULL,
    tags TEXT,
    created_at REAL NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
    content, tags, content='notes', content_rowid='id'
);
CREATE TRIGGER IF NOT EXISTS notes_ai AFTER INSERT ON notes BEGIN
    INSERT INTO notes_fts(rowid, content, tags) VALUES (new.id, new.content, new.tags);
END;
"""


def _conn() -> sqlite3.Connection:
    ensure_home()
    conn = sqlite3.connect(MEMORY_DB)
    conn.executescript(_INIT_SQL)
    return conn


async def _handler(args: dict[str, Any], ctx: ToolContext) -> str:
    content: str = args["content"]
    tags: str = ",".join(args.get("tags", []) or [])
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO notes (session_id, content, tags, created_at) VALUES (?, ?, ?, ?)",
            (ctx.session_id, content, tags, time.time()),
        )
        return f"Stored memory #{cur.lastrowid}"


register(
    Tool(
        name="memory_store",
        description=(
            "Persist a fact, preference, or learning to long-term memory. "
            "Use for things the user will care about across sessions."
        ),
        parameters={
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "The fact to remember."},
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional tags.",
                },
            },
            "required": ["content"],
        },
        permission=Permission.WRITE,
        scope=ToolScope.SYSTEM,
        handler=_handler,
    )
)
