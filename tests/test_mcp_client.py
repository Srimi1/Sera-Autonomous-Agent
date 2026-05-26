"""Tests for sera.tools.mcp — MCP client, schema conversion, registration, sampling."""
from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator

import pytest

from sera.tools.base import Permission, ToolContext, ToolScope
from sera.tools.mcp import MCPClient, MCPError, MCPServerInfo, mcp_tool_to_sera
from sera.tools.registry import all_tools, reset as reset_registry


# ---------------------------------------------------------------------------
# Helpers — mock client factory
# ---------------------------------------------------------------------------

def _make_client(responses: list[dict[str, Any]], *, sampling_llm=None) -> MCPClient:
    """Create an MCPClient in mock mode: no subprocess, responses pre-queued."""
    client = MCPClient(command=[], sampling_llm=sampling_llm)
    client._mock_responses = responses
    # Inject a fake server info so server_name works
    client._server_info = MCPServerInfo(name="test-server", version="1.0.0")
    return client


_FILESYSTEM_TOOLS = [
    {
        "name": "read_file",
        "description": "Read file contents",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "list_directory",
        "description": "List directory entries",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write file contents",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
        },
    },
]


# ---------------------------------------------------------------------------
# mcp_tool_to_sera — schema conversion
# ---------------------------------------------------------------------------

class TestMCPToolToSera:
    def _client(self) -> MCPClient:
        c = MCPClient(command=[])
        c._server_info = MCPServerInfo(name="fs", version="1.0")
        return c

    def test_name_prefixed(self) -> None:
        tool = mcp_tool_to_sera(_FILESYSTEM_TOOLS[0], self._client())
        assert tool.name == "mcp__read_file"

    def test_description_includes_server(self) -> None:
        tool = mcp_tool_to_sera(_FILESYSTEM_TOOLS[0], self._client())
        assert "[MCP:fs]" in tool.description
        assert "Read file contents" in tool.description

    def test_parameters_passthrough(self) -> None:
        tool = mcp_tool_to_sera(_FILESYSTEM_TOOLS[0], self._client())
        assert tool.parameters["type"] == "object"
        assert "path" in tool.parameters["properties"]

    def test_permission_execute(self) -> None:
        tool = mcp_tool_to_sera(_FILESYSTEM_TOOLS[0], self._client())
        assert tool.permission == Permission.EXECUTE

    def test_scope_integration(self) -> None:
        tool = mcp_tool_to_sera(_FILESYSTEM_TOOLS[0], self._client())
        assert tool.scope == ToolScope.INTEGRATION

    def test_missing_schema_defaults(self) -> None:
        mcp = {"name": "ping", "description": "ping"}
        tool = mcp_tool_to_sera(mcp, self._client())
        assert tool.parameters["type"] == "object"


# ---------------------------------------------------------------------------
# MCPClient — list_tools (mock mode)
# ---------------------------------------------------------------------------

class TestListTools:
    def test_list_returns_tools(self) -> None:
        client = _make_client([{"result": {"tools": _FILESYSTEM_TOOLS}}])
        tools = asyncio.run(client.list_tools())
        assert len(tools) == 3
        assert tools[0]["name"] == "read_file"

    def test_list_populates_internal_cache(self) -> None:
        client = _make_client([{"result": {"tools": _FILESYSTEM_TOOLS}}])
        asyncio.run(client.list_tools())
        assert len(client._mcp_tools) == 3

    def test_empty_list(self) -> None:
        client = _make_client([{"result": {"tools": []}}])
        tools = asyncio.run(client.list_tools())
        assert tools == []


# ---------------------------------------------------------------------------
# MCPClient — call_tool (mock mode)
# ---------------------------------------------------------------------------

class TestCallTool:
    def test_text_content_returned(self) -> None:
        client = _make_client([
            {"result": {"content": [{"type": "text", "text": "hello from file"}]}}
        ])
        result = asyncio.run(client.call_tool("read_file", {"path": "/tmp/f.txt"}))
        assert result == "hello from file"

    def test_multiple_text_blocks_joined(self) -> None:
        client = _make_client([
            {"result": {"content": [
                {"type": "text", "text": "line1"},
                {"type": "text", "text": "line2"},
            ]}}
        ])
        result = asyncio.run(client.call_tool("read_file", {"path": "/tmp/f.txt"}))
        assert result == "line1\nline2"

    def test_empty_content(self) -> None:
        client = _make_client([{"result": {"content": []}}])
        result = asyncio.run(client.call_tool("ping", {}))
        assert isinstance(result, str)

    def test_error_raises_mcp_error(self) -> None:
        client = _make_client([{"error": {"code": -32601, "message": "tool not found"}}])
        with pytest.raises(MCPError, match="tool not found"):
            asyncio.run(client.call_tool("missing", {}))


# ---------------------------------------------------------------------------
# MCPClient — register_tools (P-41 verification: tools appear in sera tools)
# ---------------------------------------------------------------------------

class TestRegisterTools:
    def setup_method(self) -> None:
        reset_registry()

    def teardown_method(self) -> None:
        reset_registry()

    def test_tools_appear_in_registry(self) -> None:
        client = _make_client([{"result": {"tools": _FILESYSTEM_TOOLS}}])
        asyncio.run(client.list_tools())
        count = client.register_tools()
        assert count == 3
        names = {t.name for t in all_tools()}
        assert "mcp__read_file" in names
        assert "mcp__list_directory" in names
        assert "mcp__write_file" in names

    def test_register_returns_count(self) -> None:
        client = _make_client([{"result": {"tools": _FILESYSTEM_TOOLS[:1]}}])
        asyncio.run(client.list_tools())
        assert client.register_tools() == 1

    def test_unregister_removes_tools(self) -> None:
        client = _make_client([{"result": {"tools": _FILESYSTEM_TOOLS}}])
        asyncio.run(client.list_tools())
        client.register_tools()
        removed = client.unregister_tools()
        assert removed == 3
        names = {t.name for t in all_tools()}
        assert "mcp__read_file" not in names

    def test_tool_handler_calls_call_tool(self) -> None:
        """Tool handler returned from registry calls the MCP server."""
        response = {"result": {"content": [{"type": "text", "text": "file content"}]}}
        client = _make_client(
            [{"result": {"tools": _FILESYSTEM_TOOLS[:1]}}, response]
        )
        asyncio.run(client.list_tools())
        client.register_tools()
        tool = next(t for t in all_tools() if t.name == "mcp__read_file")
        ctx = ToolContext(session_id="s1", workspace="/tmp")
        result = asyncio.run(tool.handler({"path": "/tmp/f.txt"}, ctx))
        assert result == "file content"


# ---------------------------------------------------------------------------
# Sampling — outclass: server calls Sera's LLM
# ---------------------------------------------------------------------------

class _MockLLM:
    name = "mock"
    model = "mock-model"
    context_budget = 128_000

    async def stream(self, messages, *, tools=None, system=None) -> AsyncIterator:
        from sera.llm.base import StreamChunk
        yield StreamChunk(delta_text="sampled response from Sera LLM")
        yield StreamChunk(finish_reason="stop")


class TestSampling:
    def test_sampling_without_llm_returns_placeholder(self) -> None:
        client = _make_client([])
        params = {
            "messages": [{"role": "user", "content": {"type": "text", "text": "hello"}}]
        }
        result = asyncio.run(client._handle_sampling(params))
        assert result["role"] == "assistant"
        assert "not configured" in result["content"]["text"]

    def test_sampling_with_llm_returns_response(self) -> None:
        client = _make_client([], sampling_llm=_MockLLM())
        params = {
            "messages": [{"role": "user", "content": {"type": "text", "text": "hello"}}],
            "systemPrompt": "You are helpful.",
        }
        result = asyncio.run(client._handle_sampling(params))
        assert result["role"] == "assistant"
        assert "sampled response" in result["content"]["text"]
        assert result["model"] == "mock-model"
        assert result["stopReason"] == "endTurn"

    def test_sampling_preserves_system_prompt(self) -> None:
        calls: list[dict] = []
        class _TracingLLM(_MockLLM):
            async def stream(self, messages, *, tools=None, system=None):
                calls.append({"system": system, "messages": messages})
                from sera.llm.base import StreamChunk
                yield StreamChunk(delta_text="ok")

        client = _make_client([], sampling_llm=_TracingLLM())
        asyncio.run(client._handle_sampling({
            "messages": [{"role": "user", "content": {"type": "text", "text": "q"}}],
            "systemPrompt": "Custom system",
        }))
        assert calls[0]["system"] == "Custom system"


# ---------------------------------------------------------------------------
# MCPServerInfo
# ---------------------------------------------------------------------------

class TestMCPServerInfo:
    def test_fields(self) -> None:
        info = MCPServerInfo(name="fs-server", version="1.2.3", capabilities={"tools": {}})
        assert info.name == "fs-server"
        assert info.version == "1.2.3"

    def test_server_name_property(self) -> None:
        client = _make_client([])
        assert client.server_name == "test-server"

    def test_server_name_before_start(self) -> None:
        client = MCPClient(command=[])
        assert client.server_name == "unknown"
