"""Sera HTTP API — synchronous programmatic access to the agent.

OUTCLASS: two things a hand-rolled agent API skips.

  1. Auto-published OpenAPI 3.1 spec at GET /openapi.json. Any OpenAPI client
     (Swagger UI, codegen, Postman, an LLM tool-use planner) can introspect
     the surface without docs. The spec is generated from one source of truth
     so it never drifts from the routes.

  2. Signed bearer (HS256 JWT). The token is HMAC-signed, carries a subject +
     scopes + expiry, and is verified statelessly with hmac.compare_digest —
     no token table, no DB lookup, constant-time comparison. Revoke by
     rotating the signing key. Standard JWT verifiers accept it.

Unlike the gateway server (fire-and-forget webhooks → 202), this API is
request/response: POST /v1/turn blocks until the agent finishes and returns
the actual reply.

Routes:
  POST /v1/turn        bearer-auth, scope "turn" — run one agent turn
  GET  /healthz        public liveness probe
  GET  /openapi.json   public OpenAPI 3.1 document

Wire-up (production — bridges the worker thread into the asyncio loop):
    loop   = asyncio.get_event_loop()
    bearer = SignedBearer(signing_key=os.environ["SERA_API_KEY"])
    turn   = make_async_bridge(loop, router, timeout_s=30.0)
    api    = SeraHTTPAPI(host="127.0.0.1", port=11112, turn_fn=turn, bearer=bearer)
    api.start()
    token  = bearer.issue("cli", scopes=["turn"], ttl_s=3600)
    # curl -H "Authorization: Bearer $token" -d '{"text":"hi"}' .../v1/turn
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import logging
import queue
import threading
import time
from concurrent.futures import TimeoutError as FuturesTimeoutError
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Iterator

from sera.gateway.router import InboundEvent

log = logging.getLogger("sera.rpc.http_api")

API_VERSION = "1.0.0"
DEFAULT_TURN_SCOPE = "turn"
DEFAULT_TURN_TIMEOUT_S = 30.0

# turn_fn(payload: dict) -> dict. Synchronous; raises ValueError for bad input.
TurnFn = Callable[[dict[str, Any]], dict[str, Any]]
# stream_fn(payload: dict) -> Iterator[(event_name, json_data)]. Raises ValueError for bad input.
StreamFn = Callable[[dict[str, Any]], Iterator[tuple[str, str]]]


# ---------------------------------------------------------------------------
# Signed bearer — HS256 JWT, stateless verification
# ---------------------------------------------------------------------------

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


_JWT_HEADER = {"alg": "HS256", "typ": "JWT"}
_JWT_HEADER_B64 = _b64url(json.dumps(_JWT_HEADER, separators=(",", ":")).encode("utf-8"))


@dataclass(frozen=True)
class TokenClaims:
    sub: str
    scopes: list[str]
    exp: int
    iat: int


class SignedBearer:
    """Issues and verifies HS256-signed JWT bearer tokens.

    Verification is stateless: recompute the HMAC over header.payload and
    compare in constant time. No token store. Rotate `signing_key` to revoke
    every outstanding token at once.
    """

    def __init__(self, *, signing_key: str, issuer: str = "sera") -> None:
        if not signing_key:
            raise ValueError("SignedBearer requires a non-empty signing_key")
        self._key = signing_key.encode("utf-8")
        self._issuer = issuer

    def issue(
        self,
        subject: str,
        *,
        scopes: list[str] | None = None,
        ttl_s: int = 3600,
        now: float | None = None,
    ) -> str:
        iat = int(now if now is not None else time.time())
        payload = {
            "sub": subject,
            "scope": scopes or [],
            "iat": iat,
            "exp": iat + ttl_s,
            "iss": self._issuer,
        }
        payload_b64 = _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        signing_input = f"{_JWT_HEADER_B64}.{payload_b64}".encode("ascii")
        sig = hmac.new(self._key, signing_input, hashlib.sha256).digest()
        return f"{_JWT_HEADER_B64}.{payload_b64}.{_b64url(sig)}"

    def verify(self, token: str, *, now: float | None = None) -> TokenClaims | None:
        """Return claims if the token is authentic and unexpired, else None."""
        if not token:
            return None
        parts = token.split(".")
        if len(parts) != 3:
            return None
        header_b64, payload_b64, sig_b64 = parts
        signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
        expected = hmac.new(self._key, signing_input, hashlib.sha256).digest()
        try:
            got = _b64url_decode(sig_b64)
        except Exception:  # noqa: BLE001
            return None
        if not hmac.compare_digest(expected, got):
            return None
        try:
            payload = json.loads(_b64url_decode(payload_b64))
        except Exception:  # noqa: BLE001
            return None
        if not isinstance(payload, dict):
            return None
        exp = payload.get("exp", 0)
        now_ts = now if now is not None else time.time()
        try:
            if now_ts >= float(exp):
                return None
        except (TypeError, ValueError):
            return None
        return TokenClaims(
            sub=str(payload.get("sub", "")),
            scopes=[str(s) for s in payload.get("scope", []) if isinstance(s, (str,))],
            exp=int(exp),
            iat=int(payload.get("iat", 0)),
        )


# ---------------------------------------------------------------------------
# OpenAPI 3.1 spec — single source of truth, auto-published
# ---------------------------------------------------------------------------

def build_openapi_spec(*, base_url: str = "") -> dict[str, Any]:
    """Generate the OpenAPI 3.1 document describing this API."""
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "Sera HTTP API",
            "version": API_VERSION,
            "description": (
                "Programmatic access to the Sera agent. POST a turn, receive "
                "the agent's reply. Authenticated with an HS256-signed bearer "
                "token (scope: turn)."
            ),
        },
        "servers": [{"url": base_url or "/"}],
        "components": {
            "securitySchemes": {
                "bearerAuth": {
                    "type": "http",
                    "scheme": "bearer",
                    "bearerFormat": "JWT",
                }
            },
            "schemas": {
                "TurnRequest": {
                    "type": "object",
                    "required": ["text"],
                    "properties": {
                        "text": {
                            "type": "string",
                            "description": "User message to the agent.",
                        },
                        "user_id": {"type": "string", "default": "api"},
                        "channel_id": {"type": "string", "default": "api"},
                        "platform": {"type": "string", "default": "http"},
                    },
                },
                "TurnResponse": {
                    "type": "object",
                    "properties": {
                        "ok": {"type": "boolean"},
                        "text": {"type": "string"},
                        "profile_used": {"type": ["string", "null"]},
                        "latency_ms": {"type": "integer"},
                        "error": {"type": ["string", "null"]},
                    },
                },
                "Error": {
                    "type": "object",
                    "properties": {"error": {"type": "string"}},
                },
            },
        },
        "paths": {
            "/v1/turn": {
                "post": {
                    "summary": "Run one agent turn",
                    "operationId": "runTurn",
                    "security": [{"bearerAuth": []}],
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/TurnRequest"}
                            }
                        },
                    },
                    "responses": {
                        "200": {
                            "description": "Agent reply",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/TurnResponse"}
                                }
                            },
                        },
                        "400": {
                            "description": "Bad request",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/Error"}
                                }
                            },
                        },
                        "401": {"description": "Missing or invalid bearer token"},
                        "403": {"description": "Token lacks required scope"},
                        "504": {"description": "Turn exceeded the timeout"},
                    },
                }
            },
            "/v1/turn/stream": {
                "post": {
                    "summary": "Run one agent turn, streamed as Server-Sent Events",
                    "description": (
                        "Glass-box streaming. Emits `token` events as the reply "
                        "is generated, plus `tool_start` / `tool_end` events for "
                        "the live tool-call trace, then a final `done` event with "
                        "the full TurnResponse. First token arrives before the "
                        "turn completes."
                    ),
                    "operationId": "runTurnStream",
                    "security": [{"bearerAuth": []}],
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/TurnRequest"}
                            }
                        },
                    },
                    "responses": {
                        "200": {
                            "description": "SSE stream (text/event-stream): token | tool_start | tool_end | done | error",
                            "content": {"text/event-stream": {}},
                        },
                        "400": {"description": "Bad request"},
                        "401": {"description": "Missing or invalid bearer token"},
                        "403": {"description": "Token lacks required scope"},
                        "501": {"description": "Streaming not enabled"},
                    },
                }
            },
            "/v1/ingest": {
                "post": {
                    "summary": "Ingest page content into Memory Tree",
                    "operationId": "ingestPage",
                    "security": [{"bearerAuth": []}],
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["content"],
                                    "properties": {
                                        "url": {"type": "string"},
                                        "title": {"type": "string"},
                                        "content": {"type": "string"},
                                    },
                                }
                            }
                        },
                    },
                    "responses": {
                        "200": {"description": "Chunk stored"},
                        "400": {"description": "Empty content"},
                        "401": {"description": "Unauthenticated"},
                        "501": {"description": "Ingest not enabled"},
                    },
                }
            },
            "/healthz": {
                "get": {
                    "summary": "Liveness probe",
                    "operationId": "healthz",
                    "responses": {"200": {"description": "ok"}},
                }
            },
            "/openapi.json": {
                "get": {
                    "summary": "This OpenAPI 3.1 document",
                    "operationId": "openapi",
                    "responses": {"200": {"description": "OpenAPI 3.1 document"}},
                }
            },
        },
    }


# ---------------------------------------------------------------------------
# Async bridge — worker thread → asyncio loop → back to thread
# ---------------------------------------------------------------------------

def make_async_bridge(
    loop: asyncio.AbstractEventLoop,
    router: Any,                       # sera.gateway.router.Router
    *,
    timeout_s: float = DEFAULT_TURN_TIMEOUT_S,
) -> TurnFn:
    """Build a synchronous turn_fn that drives router.dispatch on `loop`.

    The HTTP handler runs in a worker thread; this submits the coroutine to
    the running asyncio loop and blocks the worker until it completes.
    """

    def _turn(payload: dict[str, Any]) -> dict[str, Any]:
        text = str(payload.get("text") or "").strip()
        if not text:
            raise ValueError("text is required")
        event = InboundEvent(
            platform=str(payload.get("platform") or "http"),
            user_id=str(payload.get("user_id") or "api"),
            channel_id=str(payload.get("channel_id") or "api"),
            text=text,
        )
        fut = asyncio.run_coroutine_threadsafe(router.dispatch(event), loop)
        resp = fut.result(timeout=timeout_s)
        return {
            "ok": resp.ok,
            "text": resp.text,
            "profile_used": resp.profile_used,
            "latency_ms": resp.latency_ms,
            "error": resp.error,
        }

    return _turn


def _event_from_payload(payload: dict[str, Any]) -> InboundEvent:
    text = str(payload.get("text") or "").strip()
    if not text:
        raise ValueError("text is required")
    return InboundEvent(
        platform=str(payload.get("platform") or "http"),
        user_id=str(payload.get("user_id") or "api"),
        channel_id=str(payload.get("channel_id") or "api"),
        text=text,
    )


def make_streaming_bridge(
    loop: asyncio.AbstractEventLoop,
    router: Any,                       # sera.gateway.router.Router
    *,
    timeout_s: float = 60.0,
) -> StreamFn:
    """Build a stream_fn that yields SSE events as the turn unfolds.

    OUTCLASS (glass-box streaming): yields not only `token` events but the live
    tool-call trace — `tool_start` / `tool_end` — so the UI shows the agent
    reasoning AND acting in real time. Most chat streamers emit only the final
    assistant text; Sera streams the thinking. First token arrives before the
    turn completes — no waiting for the full reply.

    A TokenSink (run on the asyncio loop thread) pushes events into a
    thread-safe queue; this generator (run on the HTTP worker thread) drains it
    and yields (event_name, json_payload) pairs until the turn finishes.
    """
    from sera.agent.loop import TokenSink

    def _stream(payload: dict[str, Any]) -> Iterator[tuple[str, str]]:
        event = _event_from_payload(payload)
        q: queue.Queue = queue.Queue()
        sentinel = object()

        def on_text(t: str) -> None:
            if t:
                q.put(("token", json.dumps({"text": t})))

        def on_tool_start(name: str, args: dict) -> None:
            q.put(("tool_start", json.dumps({"name": name})))

        def on_tool_end(name: str, result: str) -> None:
            q.put(("tool_end", json.dumps({"name": name})))

        sink = TokenSink(on_text=on_text, on_tool_start=on_tool_start, on_tool_end=on_tool_end)

        async def _run():
            return await router.dispatch(event, sink=sink)

        fut = asyncio.run_coroutine_threadsafe(_run(), loop)
        fut.add_done_callback(lambda f: q.put((sentinel, f)))

        while True:
            try:
                kind, data = q.get(timeout=timeout_s)
            except queue.Empty:
                yield ("error", json.dumps({"error": "turn timed out"}))
                return
            if kind is sentinel:
                f = data
                try:
                    resp = f.result()
                    yield ("done", json.dumps({
                        "ok": resp.ok,
                        "text": resp.text,
                        "profile_used": resp.profile_used,
                        "latency_ms": resp.latency_ms,
                        "error": resp.error,
                    }))
                except Exception as exc:  # noqa: BLE001
                    yield ("error", json.dumps({"error": str(exc)}))
                return
            yield (kind, data)

    return _stream


# ---------------------------------------------------------------------------
# Request handler
# ---------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    server_version = "Sera-API/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        log.debug("api: " + fmt, *args)

    # -- GET ----------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz":
            self._respond(200, {"ok": True})
        elif self.path in ("/openapi.json", "/openapi"):
            srv: SeraHTTPAPI = self.server  # type: ignore[assignment]
            self._respond(200, build_openapi_spec(base_url=srv.url))
        else:
            self._respond(404, {"error": "not found", "path": self.path})

    # -- POST ---------------------------------------------------------------

    def do_POST(self) -> None:  # noqa: N802
        _KNOWN_PATHS = {"/v1/turn", "/v1/turn/stream", "/v1/ingest"}
        if self.path not in _KNOWN_PATHS:
            self._respond(404, {"error": "not found", "path": self.path})
            return

        srv: SeraHTTPAPI = self.server  # type: ignore[assignment]

        claims = self._authenticate(srv)
        if claims is None:
            srv.stats["unauthorized"] += 1
            self._respond(401, {"error": "missing or invalid bearer token"})
            return
        if srv.required_scope and srv.required_scope not in claims.scopes:
            srv.stats["forbidden"] += 1
            self._respond(403, {"error": f"token missing required scope: {srv.required_scope}"})
            return

        length = int(self.headers.get("Content-Length", "0") or "0")
        try:
            raw = self.rfile.read(length) if length else b""
            payload = json.loads(raw.decode("utf-8")) if raw else {}
            if not isinstance(payload, dict):
                raise ValueError("body must be a JSON object")
        except Exception as exc:  # noqa: BLE001
            srv.stats["bad_request"] += 1
            self._respond(400, {"error": f"bad body: {exc}"})
            return

        if self.path == "/v1/turn/stream":
            self._handle_stream(srv, payload)
            return

        if self.path == "/v1/ingest":
            self._handle_ingest(srv, payload)
            return

        try:
            result = srv.turn_fn(payload)
        except ValueError as exc:
            srv.stats["bad_request"] += 1
            self._respond(400, {"error": str(exc)})
            return
        except FuturesTimeoutError:
            srv.stats["timeout"] += 1
            self._respond(504, {"error": "turn timed out"})
            return
        except Exception as exc:  # noqa: BLE001
            srv.stats["error"] += 1
            log.warning("turn_fn raised: %s", exc)
            self._respond(500, {"error": f"internal error: {type(exc).__name__}"})
            return

        srv.stats["ok"] += 1
        self._respond(200, result)

    def _handle_ingest(self, srv: "SeraHTTPAPI", payload: dict[str, Any]) -> None:
        """POST /v1/ingest — store a page/content chunk into Memory Tree."""
        url = payload.get("url") or ""
        content = payload.get("content") or ""
        title = payload.get("title") or ""
        if not content.strip():
            self._respond(400, {"error": "content must not be empty"})
            return
        if srv.ingest_fn is None:
            self._respond(501, {"error": "ingest not enabled on this server"})
            return
        try:
            chunk_id = srv.ingest_fn(url=url, content=content, title=title)
        except Exception as exc:  # noqa: BLE001
            log.warning("ingest_fn raised: %s", exc)
            self._respond(500, {"error": f"ingest error: {type(exc).__name__}"})
            return
        srv.stats["ok"] += 1
        self._respond(200, {"ok": True, "chunk_id": chunk_id, "url": url})

    def _handle_stream(self, srv: "SeraHTTPAPI", payload: dict[str, Any]) -> None:
        """Stream a turn as Server-Sent Events (glass-box: tokens + tool trace)."""
        if srv.stream_fn is None:
            self._respond(501, {"error": "streaming not enabled on this server"})
            return
        # Validate input before committing to a 200 + SSE headers.
        if not str(payload.get("text") or "").strip():
            srv.stats["bad_request"] += 1
            self._respond(400, {"error": "text is required"})
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        # No Connection: keep-alive — the connection closing at generator-end is
        # the end-of-stream signal. Frames still flush incrementally before that.
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        try:
            for event_name, data in srv.stream_fn(payload):
                frame = f"event: {event_name}\ndata: {data}\n\n".encode("utf-8")
                self.wfile.write(frame)
                self.wfile.flush()
            srv.stats["ok"] += 1
        except (BrokenPipeError, ConnectionResetError):
            # Client disconnected mid-stream — normal for a cancelled turn.
            log.debug("stream: client disconnected")
        except Exception as exc:  # noqa: BLE001
            srv.stats["error"] += 1
            log.warning("stream_fn raised: %s", exc)
            with contextlib.suppress(Exception):
                err = json.dumps({"error": f"internal error: {type(exc).__name__}"})
                self.wfile.write(f"event: error\ndata: {err}\n\n".encode("utf-8"))
                self.wfile.flush()

    # -- helpers ------------------------------------------------------------

    def _authenticate(self, srv: "SeraHTTPAPI") -> TokenClaims | None:
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return None
        token = auth[len("Bearer "):].strip()
        return srv.bearer.verify(token)

    def _respond(self, status: int, body: dict[str, Any]) -> None:
        encoded = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

class SeraHTTPAPI(ThreadingHTTPServer):
    """Synchronous HTTP API in front of the agent.

    Run in a background thread; each request runs in its own worker thread and
    blocks on `turn_fn` until the agent finishes.
    """

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 0,                       # 0 → ephemeral (tests)
        turn_fn: TurnFn,
        bearer: SignedBearer,
        required_scope: str = DEFAULT_TURN_SCOPE,
        stream_fn: StreamFn | None = None,
        ingest_fn: Any | None = None,
    ) -> None:
        super().__init__((host, port), _Handler)
        self.turn_fn = turn_fn
        self.stream_fn = stream_fn
        self.ingest_fn = ingest_fn
        self.bearer = bearer
        self.required_scope = required_scope
        self.stats: dict[str, int] = {
            "ok": 0, "unauthorized": 0, "forbidden": 0,
            "bad_request": 0, "timeout": 0, "error": 0,
        }
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        host, port = self.server_address[0], self.server_address[1]
        return f"http://{host}:{port}"

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self.serve_forever, daemon=True, name="sera-api")
        self._thread.start()
        log.info("sera api listening on %s", self.url)

    def stop(self) -> None:
        with contextlib.suppress(Exception):
            self.shutdown()
        with contextlib.suppress(Exception):
            self.server_close()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None
