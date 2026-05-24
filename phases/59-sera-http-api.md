# P-59 — Sera HTTP API

## Status

done.

## Outclass claim

Two things a hand-rolled agent API skips:

1. **OpenAPI 3.1 spec auto-published** at `GET /openapi.json`, generated from
   one source of truth so it never drifts from the routes. Any OpenAPI client
   (Swagger UI, codegen, Postman, an LLM tool-use planner) can introspect the
   surface with zero docs. The served spec's `servers[0].url` matches the live
   bound address.
2. **Signed bearer (HS256 JWT).** Tokens are HMAC-signed, carry sub + scopes +
   expiry, and verify statelessly via `hmac.compare_digest` — no token table,
   constant-time comparison, standard JWT verifiers accept it. Revoke by
   rotating the signing key.

Unlike the gateway server (fire-and-forget webhooks → 202), this API is
request/response: `POST /v1/turn` blocks until the agent finishes and returns
the actual reply. The worker thread bridges into the asyncio loop via
`run_coroutine_threadsafe`.

## Files

- `sera/rpc/__init__.py`
- `sera/rpc/http_api.py` — SignedBearer, build_openapi_spec, make_async_bridge,
  SeraHTTPAPI (stdlib ThreadingHTTPServer)
- `tests/test_http_api.py` — 34 tests

## Verification

| Check | Status | Notes |
|-------|--------|-------|
| 34-test file | ✅ | bearer (13), openapi (8), http layer (11), E2E (2) |
| `curl POST /v1/turn` | ✅ | Literal curl round-trips: `{"ok":true,"text":"hello from sera",...}` |
| `curl /openapi.json` | ✅ | Returns 3.1.0 spec |
| E2E real socket + loop | ✅ | test_post_v1_turn_round_trips_through_router — urllib → bridge → Router.dispatch → run_turn |
| Signed bearer security | ✅ | forged key, tampered payload, expired, malformed all → None |
| Standard JWT | ✅ | header decodes to {"alg":"HS256","typ":"JWT"} |
| Scope enforcement | ✅ | missing "turn" scope → 403; no token → 401 |
| Full suite | ✅ | No regressions |

## Limits

**What was NOT tested:**
- TLS — API binds plaintext on localhost; the Tauri shell (P-61) talks to it
  over loopback. Remote exposure would need a TLS terminator in front.
- Streaming responses — `/v1/turn` is unary; token-by-token streaming is the
  Socket.io path in P-62, not this API.
- Concurrent turns sharing one session — each request resolves its own session
  via the Router's resolver (none by default → fresh session per call).
- Key rotation mid-flight — rotating `signing_key` invalidates live tokens
  immediately (by design), but there's no grace-window dual-key verify.
- Request body size cap — no max Content-Length; a huge body is read in full.

## Dependencies

P-51. Unblocks P-60 (unified session) and P-61 (Tauri sidecar).
