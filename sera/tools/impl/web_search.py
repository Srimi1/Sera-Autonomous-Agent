"""web_search — DuckDuckGo via ddgs lib. Read-only, no API key."""
from __future__ import annotations

import asyncio
from typing import Any

from sera.tools.base import Permission, Tool, ToolContext, ToolScope
from sera.tools.registry import register


def _search_sync(query: str, max_results: int) -> list[dict[str, Any]]:
    # Lazy import — ddgs is optional at install time for offline dev.
    from ddgs import DDGS

    with DDGS() as ddgs:
        return list(ddgs.text(query, max_results=max_results))


async def _handler(args: dict[str, Any], ctx: ToolContext) -> str:
    query: str = args["query"]
    n = int(args.get("max_results", 5))
    try:
        results = await asyncio.to_thread(_search_sync, query, n)
    except ImportError:
        return "Refused: ddgs package not installed. Run: pip install ddgs"
    except Exception as e:  # noqa: BLE001 — surface to LLM
        return f"web_search failed: {type(e).__name__}: {e}"
    if not results:
        return f"No results for: {query}"
    lines = []
    for i, r in enumerate(results, 1):
        title = r.get("title", "")
        href = r.get("href") or r.get("url", "")
        body = (r.get("body") or "").strip()
        lines.append(f"{i}. {title}\n   {href}\n   {body}")
    return "\n".join(lines)


register(
    Tool(
        name="web_search",
        description="Search the web via DuckDuckGo. Returns titles, URLs, and snippets.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query."},
                "max_results": {"type": "integer", "default": 5},
            },
            "required": ["query"],
        },
        permission=Permission.READ_ONLY,
        scope=ToolScope.SYSTEM,
        handler=_handler,
    )
)
