"""Composio dynamic action discovery — runtime action manifest.

Outclass: OpenHuman hardcodes Composio action lists. Sera calls `refresh()`
and any newly connected app's actions appear in `sera tools` immediately,
without a process restart.

Tool naming convention: composio__{app}__{action}
  e.g. GMAIL_SEND_EMAIL → composio__gmail__send_email

Usage:
    discovery = ComposioDiscovery(api_key="...")
    n = discovery.refresh(apps=["GMAIL"])   # registers N tools
    # composio__gmail__send_email is now in `sera tools`

Graceful degradation: Sera starts without the Composio SDK installed.
Run `pip install composio-openai` to enable.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from sera.tools.base import Permission, Tool, ToolContext, ToolScope
from sera.tools.registry import register, unregister

log = logging.getLogger("sera.integrations.composio")

_NOT_INSTALLED = (
    "Composio SDK not installed. Run: pip install composio-openai"
)


# ---------------------------------------------------------------------------
# Tool name derivation
# ---------------------------------------------------------------------------

def action_to_tool_name(action_name: str) -> str:
    """Convert Composio action name to Sera tool name.

    GMAIL_SEND_EMAIL → composio__gmail__send_email
    GITHUB_CREATE_ISSUE → composio__github__create_issue
    """
    return "composio__" + action_name.lower().replace("_", "__", 1)


def tool_name_to_action(tool_name: str) -> str:
    """Reverse: composio__gmail__send_email → GMAIL_SEND_EMAIL"""
    suffix = tool_name.removeprefix("composio__")
    # first __ → _ to rejoin app and action
    return suffix.replace("__", "_", 1).upper()


# ---------------------------------------------------------------------------
# Schema conversion
# ---------------------------------------------------------------------------

def _openai_schema_to_sera_params(openai_tool: dict[str, Any]) -> dict[str, Any]:
    """Extract JSON Schema parameters from an OpenAI-format tool schema."""
    fn = openai_tool.get("function", openai_tool)
    return fn.get("parameters", {"type": "object", "properties": {}})


def composio_action_to_tool(
    action: dict[str, Any],
    *,
    execute_fn: Any,
    entity_id: str = "default",
) -> Tool:
    """Convert a Composio action descriptor to a Sera Tool.

    action dict shape (normalized across SDK versions):
      {
        "name":        "GMAIL_SEND_EMAIL",
        "description": "Send an email",
        "parameters":  { JSON Schema },   # may also be nested under "function"
      }
    execute_fn: callable(action_name, params, entity_id) → str
    """
    fn_block = action.get("function", {})
    raw_name: str = action.get("name") or fn_block.get("name", "UNKNOWN")
    description: str = (
        action.get("description")
        or fn_block.get("description", "")
        or f"Composio action {raw_name}"
    )
    params = action.get("parameters") or _openai_schema_to_sera_params(action)
    tool_name = action_to_tool_name(raw_name)

    async def _handler(args: dict[str, Any], ctx: ToolContext) -> str:
        try:
            result = execute_fn(raw_name, args, entity_id=entity_id)
            if isinstance(result, dict):
                import json
                return json.dumps(result, indent=2)
            return str(result)
        except Exception as exc:  # noqa: BLE001
            return f"[composio error] {exc}"

    return Tool(
        name=tool_name,
        description=f"[Composio:{raw_name}] {description}",
        parameters=params,
        permission=Permission.EXECUTE,
        scope=ToolScope.INTEGRATION,
        handler=_handler,
    )


# ---------------------------------------------------------------------------
# Discovery engine
# ---------------------------------------------------------------------------

class ComposioDiscovery:
    """Discovers Composio actions at runtime and registers them as Sera tools.

    Call refresh() after connecting a new app; the tools appear immediately
    in `sera tools` without restarting the process.

    Args:
        api_key:    Composio API key. Defaults to COMPOSIO_API_KEY env var.
        entity_id:  Composio entity to act as. Default "default".
        _client:    Injectable mock client for testing (duck-typed).
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        entity_id: str = "default",
        _client: Any = None,
    ) -> None:
        self._api_key = api_key or os.environ.get("COMPOSIO_API_KEY", "")
        self._entity_id = entity_id
        self._client = _client
        # tool_name → raw Composio action name
        self._registered: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Client access
    # ------------------------------------------------------------------

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from composio_openai import ComposioToolSet  # type: ignore[import]
            return ComposioToolSet(api_key=self._api_key or None)
        except ImportError:
            raise RuntimeError(_NOT_INSTALLED)

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def refresh(self, apps: list[str] | None = None) -> int:
        """Fetch actions for connected apps, register as tools. Returns count added.

        Args:
            apps: List of app names to fetch (e.g. ["GMAIL", "GITHUB"]).
                  None → fetch all connected apps.
        """
        client = self._get_client()
        actions = self._fetch_actions(client, apps)

        count = 0
        for action in actions:
            try:
                execute_fn = self._make_execute(client)
                tool = composio_action_to_tool(
                    action, execute_fn=execute_fn, entity_id=self._entity_id
                )
                register(tool)
                self._registered[tool.name] = action.get("name", "")
                count += 1
                log.debug("registered %s", tool.name)
            except Exception as exc:  # noqa: BLE001
                log.warning("failed to register action %s: %s", action.get("name"), exc)

        log.info("Composio: registered %d tools from %s", count, apps or "all apps")
        return count

    def unregister_all(self) -> int:
        """Remove all registered Composio tools. Returns count removed."""
        count = sum(1 for name in self._registered if unregister(name))
        self._registered.clear()
        return count

    def registered_tools(self) -> list[str]:
        """Return names of currently registered Composio tools."""
        return list(self._registered.keys())

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _fetch_actions(self, client: Any, apps: list[str] | None) -> list[dict[str, Any]]:
        """Fetch action descriptors from the client.

        Handles both real Composio SDK and mock clients.
        """
        # Real SDK: client.get_tools(apps=[App.GMAIL]) returns OpenAI-format schemas
        try:
            if apps is not None:
                raw = client.get_tools(apps=apps)
            else:
                raw = client.get_tools()
        except TypeError:
            # Some mock clients don't accept `apps` kwarg
            raw = client.get_tools()

        # Normalize: SDK returns OpenAI-format, mocks may return flat dicts
        actions: list[dict[str, Any]] = []
        for item in raw:
            if isinstance(item, dict):
                # Already a dict — may be OpenAI format or flat Composio format
                fn = item.get("function", {})
                if fn:
                    # OpenAI-format: {"type":"function","function":{name,description,parameters}}
                    actions.append({
                        "name": fn.get("name", ""),
                        "description": fn.get("description", ""),
                        "parameters": fn.get("parameters", {}),
                    })
                else:
                    actions.append(item)
            else:
                # SDK object with attributes
                actions.append({
                    "name": getattr(item, "name", str(item)),
                    "description": getattr(item, "description", ""),
                    "parameters": getattr(item, "parameters", {}),
                })
        return actions

    def _make_execute(self, client: Any):
        """Return a callable(action_name, params, entity_id) → result."""
        def _execute(action_name: str, params: dict[str, Any], *, entity_id: str) -> Any:
            if hasattr(client, "execute_action"):
                return client.execute_action(action_name, params, entity_id=entity_id)
            raise RuntimeError("Client does not support execute_action")
        return _execute
