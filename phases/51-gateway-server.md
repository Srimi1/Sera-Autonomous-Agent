# P-51 — Gateway server

## Status

done.

## Outclass claim

**Agent-aware webhook router.** Every inbound event passes through the
Thompson bandit (P-37), the cost enforcer (P-39), the in-loop council
(P-31..P-35), and the memory-tree ingest hook (P-46) before reaching the
LLM. Hermes' gateway is a dumb dispatcher; OH/OC don't ship a gateway.
Sera's is the same brain, different inbox.

## Goal

Async HTTP webhook receiver + agent-aware router.

## Files

`sera/gateway/server.py`, `sera/gateway/router.py`.

## Verification

- `curl POST /webhook/<platform>` with JSON body → 202, event lands in
  asyncio.Queue (verified for telegram + discord + slack in one test).
- `GET /healthz` → 200 `{ok: true}`.
- `GET /stats` → 200 with accepted/rejected/bad_request counters.
- Bad JSON → 400; payload missing text → 422; unknown path → 404.
- Bandit updated end-to-end: 3 HTTP POSTs → Router.serve → bandit arm
  state shows n≥3 across `[cheap, big]` profiles.
- Hard-cap budget refuses turn before LLM is touched (`llm.calls == 0`).
- on_inbound failure does NOT block dispatch (defensive ingest hook).

## Dependencies

P-03, P-37, P-39, P-31..P-35, P-46.


## Notes

2026-05-23: stdlib-only (no aiohttp / fastapi dep). GatewayServer wraps
http.server.ThreadingHTTPServer; handler thread calls
loop.call_soon_threadsafe(queue.put_nowait, event) to bridge into asyncio.
Router.dispatch: budget check → bandit pick → llm_factory → run_turn →
bandit update with reward_signal. Router.serve(queue, max_events=N) for
controlled consumer loops. default_parser handles common JSON shapes
(text/content/body × user_id/user/from × channel_id/channel/chat_id);
platform adapters in P-52..P-55 will override with platform-specific parsers.
29 tests, 1044 total.
