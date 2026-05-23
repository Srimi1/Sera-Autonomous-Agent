"""Gateway HTTP server — receives webhooks, enqueues normalised events.

Stdlib-only (http.server.ThreadingHTTPServer). Each request runs in a worker
thread, builds an InboundEvent, and pushes it into an asyncio.Queue via
loop.call_soon_threadsafe — so the async Router consumer can pull events
without bridging threads manually.

Routes:
  POST /webhook/<platform>     accept inbound message, return 202
  GET  /healthz                liveness probe
  GET  /stats                  basic counters

Platform-specific payload parsing lives in the per-platform adapters
(P-52..P-55). The default parser pulls {user_id, channel_id, text} out of
any JSON body that has those keys.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import socket
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from sera.gateway.router import InboundEvent

log = logging.getLogger("sera.gateway.server")

# parse_<platform>(payload: dict) -> InboundEvent | None
EventParser = Callable[[str, dict[str, Any]], "InboundEvent | None"]


def default_parser(platform: str, payload: dict[str, Any]) -> InboundEvent | None:
    """Liberal default parser — picks up common keys.

    Adapters in P-52..P-55 will replace this with platform-specific parsers
    that handle Telegram's `message.from.id`, Discord's `author.id`, etc.
    """
    text = payload.get("text") or payload.get("content") or payload.get("body") or ""
    if not text:
        return None
    user_id = str(payload.get("user_id") or payload.get("user") or payload.get("from") or "anonymous")
    channel_id = str(payload.get("channel_id") or payload.get("channel") or payload.get("chat_id") or "default")
    return InboundEvent(
        platform=platform,
        user_id=user_id,
        channel_id=channel_id,
        text=text,
        metadata={"raw": payload},
    )


# ---------------------------------------------------------------------------
# Server stats
# ---------------------------------------------------------------------------

@dataclass
class ServerStats:
    accepted: int = 0
    rejected: int = 0
    bad_request: int = 0

    def as_dict(self) -> dict[str, int]:
        return {"accepted": self.accepted, "rejected": self.rejected, "bad_request": self.bad_request}


# ---------------------------------------------------------------------------
# Request handler
# ---------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    # Set by GatewayServer at bind time.
    server_version = "Sera-Gateway/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        # Quiet the default BaseHTTPRequestHandler stderr noise.
        log.debug("gateway: " + fmt, *args)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz":
            self._respond(200, {"ok": True})
        elif self.path == "/stats":
            stats = self.server.stats.as_dict()  # type: ignore[attr-defined]
            self._respond(200, stats)
        else:
            self._respond(404, {"error": "not found", "path": self.path})

    def do_POST(self) -> None:  # noqa: N802
        if not self.path.startswith("/webhook/"):
            self._respond(404, {"error": "not found", "path": self.path})
            return
        platform = self.path[len("/webhook/"):].strip("/")
        if not platform or "/" in platform:
            self._respond(400, {"error": "platform required in path: /webhook/<platform>"})
            self.server.stats.bad_request += 1  # type: ignore[attr-defined]
            return

        length = int(self.headers.get("Content-Length", "0") or "0")
        try:
            raw = self.rfile.read(length) if length else b""
            payload = json.loads(raw.decode("utf-8")) if raw else {}
            if not isinstance(payload, dict):
                raise ValueError("body must be a JSON object")
        except Exception as exc:  # noqa: BLE001
            self.server.stats.bad_request += 1  # type: ignore[attr-defined]
            self._respond(400, {"error": f"bad body: {exc}"})
            return

        srv: GatewayServer = self.server  # type: ignore[assignment]
        parser = srv.parser
        event = parser(platform, payload)
        if event is None:
            srv.stats.rejected += 1
            self._respond(422, {"error": "could not parse event from payload"})
            return

        # Bridge thread → asyncio queue.
        try:
            srv.loop.call_soon_threadsafe(srv.queue.put_nowait, event)
        except RuntimeError:
            srv.stats.rejected += 1
            self._respond(503, {"error": "router loop not running"})
            return

        srv.stats.accepted += 1
        self._respond(202, {"ok": True, "accepted": True, "platform": platform})

    def _respond(self, status: int, body: dict[str, Any]) -> None:
        encoded = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


# ---------------------------------------------------------------------------
# Server wrapper
# ---------------------------------------------------------------------------

class GatewayServer(ThreadingHTTPServer):
    """ThreadingHTTPServer that bridges into an asyncio.Queue.

    Run in a background thread; pull events from `.queue` in the asyncio loop.
    """

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 0,                  # 0 → ephemeral port (tests)
        loop: asyncio.AbstractEventLoop,
        queue: asyncio.Queue,
        parser: EventParser = default_parser,
    ) -> None:
        super().__init__((host, port), _Handler)
        self.loop = loop
        self.queue = queue
        self.parser = parser
        self.stats = ServerStats()
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        host, port = self.server_address[0], self.server_address[1]
        return f"http://{host}:{port}"

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self.serve_forever, daemon=True, name="sera-gateway")
        self._thread.start()
        log.info("gateway listening on %s", self.url)

    def stop(self) -> None:
        with contextlib.suppress(Exception):
            self.shutdown()
        with contextlib.suppress(Exception):
            self.server_close()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None


# ---------------------------------------------------------------------------
# Convenience factory — common pattern
# ---------------------------------------------------------------------------

def build_server(
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    parser: EventParser = default_parser,
    queue: asyncio.Queue | None = None,
) -> tuple[GatewayServer, asyncio.Queue]:
    """Build a GatewayServer bound to the current event loop.

    Returns (server, queue). Caller is responsible for `server.start()` and
    consuming `queue` (typically via Router.serve(queue)).
    """
    loop = asyncio.get_event_loop()
    q = queue if queue is not None else asyncio.Queue()
    return GatewayServer(host=host, port=port, loop=loop, queue=q, parser=parser), q
