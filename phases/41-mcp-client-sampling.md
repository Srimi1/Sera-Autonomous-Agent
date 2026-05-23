# P-41 — MCP client + sampling

## Status

done.

## Outclass claim

**Sampling support** — MCP servers can ask Sera for an LLM call. Few clients support it.

## Goal

Sera speaks MCP.

## Files

`sera/tools/mcp.py`.

## Verification

connect to stock MCP filesystem server; tools appear in `sera tools`.

## Dependencies

P-03.


## Notes

2026-05-23: `sera/tools/mcp.py` — MCPClient: asyncio stdio subprocess + JSON-RPC 2.0 over newline-delimited stdout. mcp_tool_to_sera() converts MCP inputSchema → Sera Tool (direct passthrough, same JSON Schema). register_tools() adds mcp__<name> tools to registry; appear in `sera tools`. call_tool() returns joined text content blocks. Sampling: _handle_sampling() calls sampling_llm.stream() on behalf of server — reverse LLM channel. MCPError on JSON-RPC error or timeout. Mock injection via _mock_responses for CI-safe tests. 23 tests, 759 total.
