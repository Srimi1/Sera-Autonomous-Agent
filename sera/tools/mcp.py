"""MCP client with sampling support.

Connects to any MCP server over stdio. Server tools appear in Sera's tool
registry alongside native tools. Sampling support lets MCP servers call
Sera's LLM — few clients implement this, making it the P-41 outclass.

Protocol: JSON-RPC 2.0 over newline-delimited stdio.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from sera.tools.base import Permission, Tool, ToolContext, ToolScope
from sera.tools.registry import register, unregister

log = logging.getLogger("sera.tools.mcp")

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class MCPError(Exception):
    """JSON-RPC or protocol error from an MCP server."""


# ---------------------------------------------------------------------------
# Server metadata
# ---------------------------------------------------------------------------

@dataclass
class MCPServerInfo:
    name: str
    version: str
    capabilities: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Schema conversion
# ---------------------------------------------------------------------------

def mcp_tool_to_sera(mcp_tool: dict[str, Any], client: "MCPClient") -> Tool:
    """Convert an MCP tool descriptor to a Sera Tool with a live handler.

    The handler calls client.call_tool() so the tool is backed by the MCP server.
    MCP inputSchema is already JSON Schema — no transformation needed.
    """
    name = mcp_tool["name"]
    description = mcp_tool.get("description", "")
    parameters = mcp_tool.get("inputSchema", {"type": "object", "properties": {}})

    async def _handler(args: dict[str, Any], ctx: ToolContext) -> str:
        return await client.call_tool(name, args)

    return Tool(
        name=f"mcp__{name}",
        description=f"[MCP:{client.server_name}] {description}",
        parameters=parameters,
        permission=Permission.EXECUTE,
        scope=ToolScope.INTEGRATION,
        handler=_handler,
    )


# ---------------------------------------------------------------------------
# MCPClient
# ---------------------------------------------------------------------------

class MCPClient:
    """Async MCP client over stdio subprocess.

    Usage:
        client = MCPClient(["npx", "-y", "@modelcontextprotocol/server-filesystem", "/tmp"])
        info = await client.start()
        n = client.register_tools()   # tools now visible in `sera tools`
        result = await client.call_tool("read_file", {"path": "/tmp/hello.txt"})
        await client.stop()

    Sampling (outclass): if sampling_llm is provided and the server sends
    sampling/createMessage, Sera's LLM handles the request transparently.
    """

    def __init__(
        self,
        command: list[str],
        *,
        sampling_llm: Any = None,
        _proc: Any = None,        # injected for testing
    ) -> None:
        self._command = command
        self._sampling_llm = sampling_llm
        self._proc: asyncio.subprocess.Process | None = _proc
        self._req_id = 0
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._mcp_tools: list[dict[str, Any]] = []
        self._server_info: MCPServerInfo | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._registered: list[str] = []
        self._lock = asyncio.Lock()
        # For test injection: pre-cooked response queue
        self._mock_responses: list[dict[str, Any]] = []

    @property
    def server_name(self) -> str:
        return self._server_info.name if self._server_info else "unknown"

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> MCPServerInfo:
        """Start subprocess, complete MCP handshake, return server info."""
        if self._proc is None:
            self._proc = await asyncio.create_subprocess_exec(
                *self._command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        self._reader_task = asyncio.create_task(self._reader_loop())

        # MCP initialize handshake
        result = await self._send("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {
                "sampling": {} if self._sampling_llm else {},
            },
            "clientInfo": {"name": "sera", "version": "0.1.0"},
        })
        info_raw = result.get("serverInfo", {})
        self._server_info = MCPServerInfo(
            name=info_raw.get("name", "mcp-server"),
            version=info_raw.get("version", "0.0.0"),
            capabilities=result.get("capabilities", {}),
        )
        await self._notify("notifications/initialized", {})
        log.info("MCP server %s %s ready", self._server_info.name, self._server_info.version)
        return self._server_info

    async def stop(self) -> None:
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
        if self._proc and self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                self._proc.kill()
        self.unregister_tools()

    # ------------------------------------------------------------------
    # Tool operations
    # ------------------------------------------------------------------

    async def list_tools(self) -> list[dict[str, Any]]:
        """Fetch tool list from the server."""
        result = await self._send("tools/list", {})
        self._mcp_tools = result.get("tools", [])
        return list(self._mcp_tools)

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        """Call a tool by its original MCP name (not mcp__-prefixed)."""
        result = await self._send("tools/call", {"name": name, "arguments": arguments})
        # MCP content is a list of content blocks
        content = result.get("content", [])
        parts: list[str] = []
        for block in content:
            if block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif block.get("type") == "resource":
                parts.append(str(block.get("resource", {}).get("text", "")))
        return "\n".join(parts) if parts else str(result)

    def register_tools(self, *, permission: Permission = Permission.EXECUTE) -> int:
        """Register all listed tools into Sera's tool registry. Returns count added."""
        count = 0
        for mcp_tool in self._mcp_tools:
            tool = mcp_tool_to_sera(mcp_tool, self)
            if permission != Permission.EXECUTE:
                # Override default execute permission if requested
                object.__setattr__(tool, "permission", permission)
            register(tool)
            self._registered.append(tool.name)
            count += 1
        return count

    def unregister_tools(self) -> int:
        """Remove previously registered MCP tools from the registry."""
        count = sum(1 for n in self._registered if unregister(n))
        self._registered.clear()
        return count

    # ------------------------------------------------------------------
    # JSON-RPC transport
    # ------------------------------------------------------------------

    async def _send(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Send a JSON-RPC request and await the response dict."""
        async with self._lock:
            self._req_id += 1
            req_id = self._req_id

        # Test injection: return mock response if available
        if self._mock_responses:
            _resp = self._mock_responses.pop(0)
            if "error" in _resp:
                raise MCPError(_resp["error"].get("message", "RPC error"))
            return _resp.get("result", {})

        if self._proc is None or self._proc.stdin is None:
            raise MCPError("MCP client not started")

        fut: asyncio.Future[dict[str, Any]] = asyncio.get_event_loop().create_future()
        self._pending[req_id] = fut

        msg = json.dumps({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
        self._proc.stdin.write((msg + "\n").encode())
        await self._proc.stdin.drain()

        try:
            return await asyncio.wait_for(fut, timeout=30.0)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            raise MCPError(f"Timeout waiting for {method} response")

    async def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        """Send a JSON-RPC notification (no response expected)."""
        if self._mock_responses is not None and self._proc is None:
            return  # mock mode — notifications are no-ops

        if self._proc and self._proc.stdin:
            msg = json.dumps({"jsonrpc": "2.0", "method": method, "params": params or {}})
            self._proc.stdin.write((msg + "\n").encode())
            await self._proc.stdin.drain()

    async def _reader_loop(self) -> None:
        """Read stdout line-by-line; dispatch responses and server requests."""
        if self._proc is None or self._proc.stdout is None:
            return
        try:
            async for line in self._proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg: dict[str, Any] = json.loads(line)
                except json.JSONDecodeError:
                    log.warning("MCP: bad JSON from server: %s", line[:100])
                    continue
                await self._dispatch(msg)
        except asyncio.CancelledError:
            pass
        except Exception as e:  # noqa: BLE001
            log.error("MCP reader loop error: %s", e)

    async def _dispatch(self, msg: dict[str, Any]) -> None:
        """Route a server message to a pending future or server-request handler."""
        msg_id = msg.get("id")
        if msg_id is not None and msg_id in self._pending:
            fut = self._pending.pop(msg_id)
            if "error" in msg:
                fut.set_exception(MCPError(msg["error"].get("message", "RPC error")))
            else:
                fut.set_result(msg.get("result", {}))
        elif "method" in msg:
            # Server-initiated request (e.g. sampling)
            asyncio.create_task(self._handle_server_request(msg))

    # ------------------------------------------------------------------
    # Sampling — outclass: server asks Sera's LLM for a response
    # ------------------------------------------------------------------

    async def _handle_server_request(self, msg: dict[str, Any]) -> None:
        """Dispatch server-initiated requests. Sampling is the main case."""
        method = msg.get("method", "")
        msg_id = msg.get("id")

        if method == "sampling/createMessage":
            response = await self._handle_sampling(msg.get("params", {}))
            if msg_id is not None and self._proc and self._proc.stdin:
                reply = json.dumps({"jsonrpc": "2.0", "id": msg_id, "result": response})
                self._proc.stdin.write((reply + "\n").encode())
                await self._proc.stdin.drain()
        else:
            log.debug("MCP: unhandled server method %s", method)

    async def _handle_sampling(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle sampling/createMessage — call Sera's LLM on behalf of the server.

        This is the P-41 outclass: few MCP clients implement the sampling
        capability, which lets servers request LLM calls from the client.
        """
        if self._sampling_llm is None:
            return {
                "role": "assistant",
                "content": {"type": "text", "text": "[sampling not configured]"},
                "model": "none",
                "stopReason": "endTurn",
            }

        messages = params.get("messages", [])
        system = params.get("systemPrompt")
        max_tokens = params.get("maxTokens", 1024)

        # Convert MCP sampling messages to OpenAI format
        openai_msgs = [
            {"role": m.get("role", "user"), "content": m.get("content", {}).get("text", "")}
            for m in messages
        ]

        text = ""
        async for chunk in self._sampling_llm.stream(
            messages=openai_msgs,
            tools=None,
            system=system,
        ):
            text += chunk.delta_text

        return {
            "role": "assistant",
            "content": {"type": "text", "text": text},
            "model": getattr(self._sampling_llm, "model", "sera"),
            "stopReason": "endTurn",
        }
