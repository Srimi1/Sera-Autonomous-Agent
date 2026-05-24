"""P-95: mobile sync client — chat/ingest over the sidecar + offline CRDT merge.

The transport is injectable (`Callable[[method, path, payload, headers], dict]`)
so the client is testable without a live sidecar. In production the transport is
a thin HTTP/gRPC wrapper; here we drive it with a fake server in tests.

Offline edits accumulate in a local CRDTDocument; `sync()` ships local state to
the relay, merges the remote state back, and converges deterministically.
"""
from __future__ import annotations

from typing import Any, Callable

from sera.sync.crdt import CRDTDocument, RelayStub

# method, path, json-payload, headers -> json-response
Transport = Callable[[str, str, dict, dict], dict]


class TransportError(RuntimeError):
    """Raised when the transport reports a non-success or the client is offline."""


class MobileSyncClient:
    """Phone-side client: same core API as desktop, plus offline CRDT memory."""

    def __init__(
        self,
        *,
        server_url: str,
        token: str,
        node_id: str = "mobile",
        transport: Transport | None = None,
    ) -> None:
        self.server_url = server_url.rstrip("/")
        self.token = token
        self.node_id = node_id
        self._transport = transport or self._default_transport
        self.doc = CRDTDocument()
        self._relay = RelayStub()
        self.online = True

    # -- core API: same endpoints the desktop shell uses --------------------

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}

    def _call(self, method: str, path: str, payload: dict) -> dict:
        url = f"{self.server_url}{path}"
        resp = self._transport(method, url, payload, self._headers())
        if not isinstance(resp, dict) or resp.get("error"):
            raise TransportError(str(resp.get("error") if isinstance(resp, dict) else resp))
        return resp

    def chat(self, text: str) -> str:
        """Run one agent turn through the shared sidecar. Returns the reply text."""
        resp = self._call("POST", "/v1/turn", {"text": text})
        return resp.get("reply", "")

    def ingest(self, text: str, *, source: str = "mobile") -> str:
        """Store content into the shared Memory Tree. Returns the chunk id."""
        resp = self._call("POST", "/v1/ingest", {"text": text, "source": source})
        return resp.get("chunk_id", "")

    # -- offline-first memory: CRDT --------------------------------------

    def remember_chunk(self, chunk_id: str) -> None:
        """Record a chunk locally — survives offline, merges on sync."""
        self.doc.chunks.add(chunk_id, node_id=self.node_id)

    def set_entity(self, key: str, value: Any) -> None:
        self.doc.entities.set(key, value, node_id=self.node_id)

    def sync(self) -> CRDTDocument:
        """Push local CRDT state to the relay, merge remote state back."""
        if not self.online:
            raise TransportError("offline — cannot sync")
        local_payload = self._relay.push(self.doc)
        resp = self._call("POST", "/v1/sync", {"crdt": local_payload.decode()})
        remote_raw = resp.get("crdt")
        if remote_raw:
            remote_doc = self._relay.pull(remote_raw.encode())
            self.doc.merge(remote_doc)
        return self.doc

    # -- default (real) transport ----------------------------------------

    @staticmethod
    def _default_transport(method: str, url: str, payload: dict, headers: dict) -> dict:
        import json
        import urllib.request

        data = json.dumps(payload).encode()
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        for k, v in headers.items():
            req.add_header(k, v)
        with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 — local sidecar
            return json.loads(r.read().decode())
