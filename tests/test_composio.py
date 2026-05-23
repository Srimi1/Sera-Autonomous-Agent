"""Tests for sera.integrations.composio — dynamic Composio action discovery."""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from sera.integrations.composio import (
    ComposioDiscovery,
    action_to_tool_name,
    composio_action_to_tool,
    tool_name_to_action,
)
from sera.tools.base import Permission, ToolContext, ToolScope
from sera.tools.registry import all_tools, reset as reset_registry


# ---------------------------------------------------------------------------
# Mock Composio client
# ---------------------------------------------------------------------------

_GMAIL_ACTIONS = [
    {
        "name": "GMAIL_SEND_EMAIL",
        "description": "Send an email via Gmail",
        "parameters": {
            "type": "object",
            "properties": {
                "recipient_email": {"type": "string"},
                "subject": {"type": "string"},
                "body": {"type": "string"},
            },
            "required": ["recipient_email", "subject", "body"],
        },
    },
    {
        "name": "GMAIL_FETCH_EMAILS",
        "description": "Fetch emails from Gmail inbox",
        "parameters": {
            "type": "object",
            "properties": {
                "max_results": {"type": "integer", "default": 10},
            },
            "required": [],
        },
    },
]

_GITHUB_ACTIONS = [
    {
        "name": "GITHUB_CREATE_ISSUE",
        "description": "Create a GitHub issue",
        "parameters": {
            "type": "object",
            "properties": {
                "owner": {"type": "string"},
                "repo": {"type": "string"},
                "title": {"type": "string"},
            },
            "required": ["owner", "repo", "title"],
        },
    },
]


class _MockClient:
    """Duck-typed Composio client for testing."""

    def __init__(self, actions: list[dict]) -> None:
        self._actions = actions
        self.executed: list[tuple[str, dict, str]] = []

    def get_tools(self, apps: list[str] | None = None) -> list[dict]:
        if apps is None:
            return self._actions
        # Filter by app prefix
        prefix_set = {a.upper() for a in apps}
        return [a for a in self._actions if a["name"].split("_")[0] in prefix_set]

    def execute_action(self, action_name: str, params: dict, *, entity_id: str = "default") -> dict:
        self.executed.append((action_name, params, entity_id))
        return {"success": True, "data": f"executed {action_name}"}


def _discovery(actions: list[dict] | None = None, *, both: bool = False) -> ComposioDiscovery:
    if both:
        all_actions = _GMAIL_ACTIONS + _GITHUB_ACTIONS
    else:
        all_actions = actions or _GMAIL_ACTIONS
    return ComposioDiscovery(_client=_MockClient(all_actions))


# ---------------------------------------------------------------------------
# action_to_tool_name / tool_name_to_action
# ---------------------------------------------------------------------------

class TestToolNameConversion:
    def test_gmail_send_email(self) -> None:
        assert action_to_tool_name("GMAIL_SEND_EMAIL") == "composio__gmail__send_email"

    def test_github_create_issue(self) -> None:
        assert action_to_tool_name("GITHUB_CREATE_ISSUE") == "composio__github__create_issue"

    def test_slack_send_message(self) -> None:
        assert action_to_tool_name("SLACK_SEND_MESSAGE") == "composio__slack__send_message"

    def test_reverse_gmail(self) -> None:
        assert tool_name_to_action("composio__gmail__send_email") == "GMAIL_SEND_EMAIL"

    def test_reverse_github(self) -> None:
        assert tool_name_to_action("composio__github__create_issue") == "GITHUB_CREATE_ISSUE"

    def test_round_trip(self) -> None:
        for name in ["GMAIL_SEND_EMAIL", "GITHUB_CREATE_ISSUE", "SLACK_SEND_MESSAGE"]:
            assert tool_name_to_action(action_to_tool_name(name)) == name


# ---------------------------------------------------------------------------
# composio_action_to_tool
# ---------------------------------------------------------------------------

class TestActionToTool:
    def _exec(self):
        results: list[dict] = []
        def _fn(name, params, *, entity_id):
            results.append({"name": name, "params": params})
            return {"success": True, "data": "ok"}
        return _fn, results

    def test_tool_name(self) -> None:
        fn, _ = self._exec()
        tool = composio_action_to_tool(_GMAIL_ACTIONS[0], execute_fn=fn)
        assert tool.name == "composio__gmail__send_email"

    def test_description_includes_action_name(self) -> None:
        fn, _ = self._exec()
        tool = composio_action_to_tool(_GMAIL_ACTIONS[0], execute_fn=fn)
        assert "GMAIL_SEND_EMAIL" in tool.description
        assert "Send an email" in tool.description

    def test_parameters_passthrough(self) -> None:
        fn, _ = self._exec()
        tool = composio_action_to_tool(_GMAIL_ACTIONS[0], execute_fn=fn)
        assert "recipient_email" in tool.parameters["properties"]

    def test_permission_execute(self) -> None:
        fn, _ = self._exec()
        tool = composio_action_to_tool(_GMAIL_ACTIONS[0], execute_fn=fn)
        assert tool.permission == Permission.EXECUTE

    def test_scope_integration(self) -> None:
        fn, _ = self._exec()
        tool = composio_action_to_tool(_GMAIL_ACTIONS[0], execute_fn=fn)
        assert tool.scope == ToolScope.INTEGRATION

    def test_handler_calls_execute(self) -> None:
        fn, calls = self._exec()
        tool = composio_action_to_tool(_GMAIL_ACTIONS[0], execute_fn=fn)
        ctx = ToolContext(session_id="s", workspace="/tmp")
        asyncio.run(tool.handler({"recipient_email": "a@b.com", "subject": "Hi", "body": "Hello"}, ctx))
        assert len(calls) == 1
        assert calls[0]["name"] == "GMAIL_SEND_EMAIL"

    def test_handler_returns_json(self) -> None:
        fn, _ = self._exec()
        tool = composio_action_to_tool(_GMAIL_ACTIONS[0], execute_fn=fn)
        ctx = ToolContext(session_id="s", workspace="/tmp")
        result = asyncio.run(tool.handler({}, ctx))
        # Result should be JSON or string
        assert isinstance(result, str)

    def test_openai_format_passthrough(self) -> None:
        """Handles OpenAI-format action schema (real SDK output)."""
        fn, _ = self._exec()
        openai_action = {
            "type": "function",
            "function": {
                "name": "GMAIL_SEND_EMAIL",
                "description": "Send email",
                "parameters": {"type": "object", "properties": {"to": {"type": "string"}}},
            },
        }
        tool = composio_action_to_tool(openai_action, execute_fn=fn)
        assert tool.name == "composio__gmail__send_email"


# ---------------------------------------------------------------------------
# ComposioDiscovery.refresh — P-45 verification criterion
# ---------------------------------------------------------------------------

class TestComposioDiscovery:
    def setup_method(self) -> None:
        reset_registry()

    def teardown_method(self) -> None:
        reset_registry()

    def test_refresh_returns_count(self) -> None:
        d = _discovery()
        count = d.refresh()
        assert count == 2  # 2 Gmail actions

    def test_gmail_tools_appear_in_registry(self) -> None:
        """Verification: composio__gmail__send_email appears in sera tools after refresh."""
        d = _discovery()
        d.refresh()
        names = {t.name for t in all_tools()}
        assert "composio__gmail__send_email" in names
        assert "composio__gmail__fetch_emails" in names

    def test_refresh_with_app_filter(self) -> None:
        d = _discovery(both=True)
        count = d.refresh(apps=["GMAIL"])
        assert count == 2
        names = {t.name for t in all_tools()}
        assert "composio__gmail__send_email" in names
        assert "composio__github__create_issue" not in names

    def test_refresh_all_apps(self) -> None:
        d = _discovery(both=True)
        d.refresh()
        names = {t.name for t in all_tools()}
        assert "composio__gmail__send_email" in names
        assert "composio__github__create_issue" in names

    def test_registered_tools_list(self) -> None:
        d = _discovery()
        d.refresh()
        registered = d.registered_tools()
        assert "composio__gmail__send_email" in registered

    def test_unregister_all_removes_tools(self) -> None:
        d = _discovery()
        d.refresh()
        removed = d.unregister_all()
        assert removed == 2
        names = {t.name for t in all_tools()}
        assert "composio__gmail__send_email" not in names

    def test_tools_registered_without_restart(self) -> None:
        """Connect Gmail → tools appear immediately (no restart required)."""
        d = _discovery()
        # Before refresh: no composio tools
        names_before = {t.name for t in all_tools()}
        assert "composio__gmail__send_email" not in names_before
        # After refresh: tools appear
        d.refresh()
        names_after = {t.name for t in all_tools()}
        assert "composio__gmail__send_email" in names_after

    def test_second_refresh_re_registers(self) -> None:
        d = _discovery()
        d.refresh()
        # Refresh again — tools already registered; re-registration is idempotent
        count2 = d.refresh()
        assert count2 == 2
        names = {t.name for t in all_tools()}
        assert "composio__gmail__send_email" in names

    def test_tool_handler_executes_action(self) -> None:
        client = _MockClient(_GMAIL_ACTIONS)
        d = ComposioDiscovery(_client=client)
        d.refresh()
        tool = next(t for t in all_tools() if t.name == "composio__gmail__send_email")
        ctx = ToolContext(session_id="s", workspace="/tmp")
        result = asyncio.run(tool.handler({
            "recipient_email": "test@test.com",
            "subject": "Hello",
            "body": "World",
        }, ctx))
        assert len(client.executed) == 1
        assert client.executed[0][0] == "GMAIL_SEND_EMAIL"
        assert "success" in result.lower() or "executed" in result.lower()

    def test_missing_sdk_raises_runtime_error(self) -> None:
        """Without injectable client, missing SDK raises RuntimeError."""
        d = ComposioDiscovery(api_key="fake-key")  # no _client injected
        with pytest.raises(RuntimeError, match="not installed|composio"):
            d.refresh()


# ---------------------------------------------------------------------------
# OpenAI-format action schema (real SDK compatibility)
# ---------------------------------------------------------------------------

class TestOpenAIFormatActions:
    def setup_method(self) -> None:
        reset_registry()

    def teardown_method(self) -> None:
        reset_registry()

    def test_openai_format_parsed(self) -> None:
        openai_actions = [
            {
                "type": "function",
                "function": {
                    "name": "GMAIL_SEND_EMAIL",
                    "description": "Send email via Gmail",
                    "parameters": {
                        "type": "object",
                        "properties": {"to": {"type": "string"}},
                        "required": ["to"],
                    },
                },
            }
        ]

        class _OAIClient:
            def get_tools(self, apps=None):
                return openai_actions

            def execute_action(self, name, params, *, entity_id):
                return {"ok": True}

        d = ComposioDiscovery(_client=_OAIClient())
        count = d.refresh()
        assert count == 1
        names = {t.name for t in all_tools()}
        assert "composio__gmail__send_email" in names
