# P-62 — Chat panel + streaming

## Status

done.

## Outclass claim

**Glass-box streaming over zero-dependency SSE.** Two rejections of the
blueprint:

1. **Not Socket.io — Server-Sent Events.** SSE is the right tool for
   server→client token streaming: stdlib-only (a streaming HTTP response with
   `text/event-stream`), no extra dependency, natively consumable from the
   shell. OpenHuman's Socket.io is a heavier bidirectional protocol we don't
   need. We do not bend our shape to theirs.
2. **Glass-box, not just tokens.** Sera streams the *live tool-call trace* —
   `tool_start` / `tool_end` events alongside `token` events — so the UI shows
   the agent reasoning AND acting in real time. Most chat streamers emit only
   the final assistant text. Sera streams the thinking. Verified:
   `test_tool_events_stream`.

First token arrives before the turn completes (verified:
`test_first_token_arrives_before_turn_done` — first token lands ≥0.4s ahead of
a deliberately-stalled completion), which is the property the blueprint's
"<100ms first token" depends on. The actual shell-measured p50 needs the Tauri
runtime (deferred — see Limits).

## Files

- `sera/rpc/http_api.py` — make_streaming_bridge (SSE bridge), `/v1/turn/stream`
  route, OpenAPI entry, StreamFn type
- `sera/gateway/router.py` — `dispatch(event, *, sink=...)` threads a TokenSink
  into run_turn
- `sera/rpc/server.py` — wires stream_fn into boot_sidecar
- `sera-shell/src/components/Chat.tsx` — fetch-streaming SSE consumer (renders
  tokens + tool trace)
- `tests/test_http_stream.py` — 11 tests

## Verification

| Check | Status | Notes |
|-------|--------|-------|
| 11 tests | ✅ | bridge ordering, glass-box tools, first-token latency, E2E SSE, auth, 501, OpenAPI |
| **Glass-box tool trace** | ✅ | tool_start/tool_end stream before done; tool named |
| First-token-before-done | ✅ | first token ≥0.4s ahead of stalled completion |
| Real-socket SSE | ✅ | urllib reads event:/data: frames; tokens reassemble to full text |
| Live `curl -N` stream | ✅ | `sera serve` streams `event: done` frame over real curl; endpoint in /openapi.json |
| Auth on stream | ✅ | no token → 401; stream_fn absent → 501 |
| Full suite | ✅ | No regressions (router.dispatch sink param is backward-compatible) |

## Limits

**What was NOT verified:**
- **Shell-measured p50 first-token latency** — the blueprint's literal metric
  needs `pnpm tauri dev` + a real provider key; no cargo here. The *streaming
  capability* that makes low first-token latency possible is built and tested;
  the wall-clock number in the shell is deferred to a toolchain machine.
- **Chat.tsx is not run** — written as a real fetch-streaming SSE consumer but
  not executed (no Tauri/Vite here). The Python streaming contract it depends
  on is fully tested.
- **Mid-stream interrupt** — closing the connection is caught
  (BrokenPipeError) but does not yet propagate an InterruptToken into the
  running turn to actually cancel LLM work. Connection-close → cancel is a
  follow-up.
- **Backpressure** — if the client reads slower than tokens are produced, the
  queue in make_streaming_bridge grows unbounded. Fine for interactive chat;
  would need a bound for adversarial clients.

## Dependencies

P-61.
