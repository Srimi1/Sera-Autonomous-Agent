"""P-91 transport: real-time CRDT relay over WebSockets.

OUTCLASS: rivals sync memory on explicit push to a central store — last writer
clobbers, offline edits are lost. Sera runs a WebSocket relay that broadcasts
CRDT deltas between the user's devices. Phone writes a chunk; the laptop sees
it in well under a second; concurrent edits converge deterministically by the
CRDT math (LWWRegister + ORSet). No central authority owns the truth — the
relay is a dumb broadcast hub, and every node holds a complete, mergeable copy.

Wire protocol (JSON, one message per frame):
    {"v": 1, "doc": <CRDTDocument.to_dict()>}

Flow:
    - A node connects → relay sends it the merged authoritative state.
    - A node sends its local doc → relay merges it, rebroadcasts merged state
      to every connected node (including the sender; merge is idempotent).
    - Convergence is monotone: state only grows toward the join of all docs.

The relay holds a merged CRDTDocument purely so late-joiners get full history
in one frame. It is NOT a source of truth — kill it and the nodes still hold
everything. Restart it and the next push re-seeds it.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable

from sera.sync.crdt import CRDTDocument

log = logging.getLogger("sera.sync.relay")

PROTOCOL_VERSION = 1


def encode_doc(doc: CRDTDocument) -> str:
    return json.dumps({"v": PROTOCOL_VERSION, "doc": doc.to_dict()})


def decode_doc(payload: str | bytes) -> CRDTDocument:
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8")
    obj = json.loads(payload)
    return CRDTDocument.from_dict(obj.get("doc", {}))


# ---------------------------------------------------------------------------
# RelayServer — broadcast hub holding merged authoritative state
# ---------------------------------------------------------------------------

class RelayServer:
    """Async WebSocket relay. Merges incoming docs, rebroadcasts merged state."""

    def __init__(self, *, host: str = "127.0.0.1", port: int = 0) -> None:
        self._host = host
        self._port = port
        self._doc = CRDTDocument()
        self._clients: set[Any] = set()
        self._server: Any = None
        self._lock = asyncio.Lock()

    @property
    def doc(self) -> CRDTDocument:
        """The merged authoritative document (join of everything seen)."""
        return self._doc

    @property
    def n_clients(self) -> int:
        return len(self._clients)

    @property
    def port(self) -> int:
        """Bound port — resolves the ephemeral port after start()."""
        if self._server is not None and self._server.sockets:
            return self._server.sockets[0].getsockname()[1]
        return self._port

    @property
    def url(self) -> str:
        return f"ws://{self._host}:{self.port}"

    async def _handler(self, ws: Any) -> None:
        self._clients.add(ws)
        try:
            # Seed the new client with current merged state.
            await ws.send(encode_doc(self._doc))
            async for raw in ws:
                try:
                    incoming = decode_doc(raw)
                except (ValueError, KeyError, TypeError) as exc:
                    log.warning("relay: dropping malformed frame: %s", exc)
                    continue
                async with self._lock:
                    self._doc.merge(incoming)
                    snapshot = encode_doc(self._doc)
                await self._broadcast(snapshot)
        finally:
            self._clients.discard(ws)

    async def _broadcast(self, payload: str) -> None:
        if not self._clients:
            return
        results = await asyncio.gather(
            *(c.send(payload) for c in list(self._clients)),
            return_exceptions=True,
        )
        for r in results:
            if isinstance(r, Exception):
                log.debug("relay: broadcast to one client failed: %s", r)

    async def start(self) -> "RelayServer":
        from websockets.asyncio.server import serve

        self._server = await serve(self._handler, self._host, self._port)
        log.info("CRDT relay listening on %s", self.url)
        return self

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None


# ---------------------------------------------------------------------------
# RelayClient — connect, push local doc, merge inbound state
# ---------------------------------------------------------------------------

class RelayClient:
    """Connects a local CRDTDocument to a relay and keeps it converged."""

    def __init__(
        self,
        uri: str,
        *,
        doc: CRDTDocument | None = None,
        on_update: Callable[[CRDTDocument], Awaitable[None] | None] | None = None,
    ) -> None:
        self._uri = uri
        self.doc = doc or CRDTDocument()
        self._on_update = on_update
        self._ws: Any = None
        self._recv_task: asyncio.Task | None = None
        self._first_state = asyncio.Event()

    async def connect(self) -> "RelayClient":
        from websockets.asyncio.client import connect

        self._ws = await connect(self._uri)
        self._recv_task = asyncio.ensure_future(self._recv_loop())
        return self

    async def _recv_loop(self) -> None:
        try:
            async for raw in self._ws:
                try:
                    incoming = decode_doc(raw)
                except (ValueError, KeyError, TypeError):
                    continue
                self.doc.merge(incoming)
                self._first_state.set()
                if self._on_update is not None:
                    res = self._on_update(self.doc)
                    if asyncio.iscoroutine(res):
                        await res
        except Exception as exc:  # noqa: BLE001 — connection closed / reset
            log.debug("relay client recv loop ended: %s", exc)

    async def push(self) -> None:
        """Send the local doc to the relay."""
        if self._ws is None:
            raise RuntimeError("RelayClient.push before connect()")
        await self._ws.send(encode_doc(self.doc))

    async def wait_for_state(self, timeout: float = 5.0) -> None:
        """Block until at least one inbound merge has landed."""
        await asyncio.wait_for(self._first_state.wait(), timeout=timeout)

    async def sync(self, timeout: float = 5.0) -> CRDTDocument:
        """Push local state and wait for the relay's merged broadcast to land."""
        self._first_state.clear()
        await self.push()
        await self.wait_for_state(timeout=timeout)
        return self.doc

    async def close(self) -> None:
        if self._recv_task is not None:
            self._recv_task.cancel()
            try:
                await self._recv_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._recv_task = None
        if self._ws is not None:
            await self._ws.close()
            self._ws = None


# ---------------------------------------------------------------------------
# Convenience: run a standalone relay (the "relay binary" from the phase spec)
# ---------------------------------------------------------------------------

async def serve_forever(host: str = "127.0.0.1", port: int = 8787) -> None:
    server = await RelayServer(host=host, port=port).start()
    try:
        await asyncio.Future()  # run until cancelled
    finally:
        await server.stop()
