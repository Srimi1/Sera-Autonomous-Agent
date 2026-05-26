"""P-91: Yjs-style CRDT for Sera memory — chunks, entities, relations.

OUTCLASS: Rivals sync on explicit push. Sera stores Yjs-compatible CRDT state
so concurrent edits from N devices (phone, laptop, desktop) converge
deterministically — no central authority, no merge conflicts, no lost writes.

Two primitives:
  - LWWRegister: last-write-wins single value keyed by (node_id, key).
  - ORSet: observed-remove set — concurrent adds always win over removes.

A CRDTDocument wraps both for the three Sera namespaces: chunks, entities,
relations.  merge() is commutative, associative, and idempotent.

RelayStub serialises/deserialises the document as JSON — kept for in-process
round-trip tests. The live cross-device transport ships in sera/sync/relay.py
(RelayServer + RelayClient over WebSockets); run it with `sera relay`.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from typing import Any


# ---------------------------------------------------------------------------
# LWWRegister — Last-Write-Wins scalar register
# ---------------------------------------------------------------------------

@dataclass
class LWWEntry:
    value: Any
    ts: float      # monotonic epoch seconds
    node_id: str   # tiebreak: higher lexicographic wins


def _lww_wins(a: LWWEntry, b: LWWEntry) -> LWWEntry:
    if a.ts > b.ts:
        return a
    if b.ts > a.ts:
        return b
    return a if a.node_id >= b.node_id else b


class LWWRegister:
    """Map of key → last-write-wins value."""

    def __init__(self) -> None:
        self._store: dict[str, LWWEntry] = {}

    def set(self, key: str, value: Any, *, node_id: str, ts: float | None = None) -> None:
        entry = LWWEntry(value=value, ts=ts if ts is not None else time.time(), node_id=node_id)
        existing = self._store.get(key)
        self._store[key] = _lww_wins(entry, existing) if existing else entry

    def get(self, key: str, default: Any = None) -> Any:
        entry = self._store.get(key)
        return entry.value if entry else default

    def keys(self) -> list[str]:
        return list(self._store.keys())

    def merge(self, other: "LWWRegister") -> None:
        for key, entry in other._store.items():
            existing = self._store.get(key)
            self._store[key] = _lww_wins(entry, existing) if existing else entry

    def to_dict(self) -> dict:
        return {
            k: {"value": v.value, "ts": v.ts, "node_id": v.node_id}
            for k, v in self._store.items()
        }

    @classmethod
    def from_dict(cls, data: dict) -> "LWWRegister":
        reg = cls()
        for key, entry in data.items():
            reg._store[key] = LWWEntry(
                value=entry["value"],
                ts=entry["ts"],
                node_id=entry["node_id"],
            )
        return reg


# ---------------------------------------------------------------------------
# ORSet — Observed-Remove Set
# ---------------------------------------------------------------------------

@dataclass
class ORSetEntry:
    uid: str       # unique per add operation
    node_id: str
    ts: float


class ORSet:
    """Observed-remove set: concurrent adds always survive concurrent removes."""

    def __init__(self) -> None:
        self._adds: dict[str, set[str]] = {}    # element → {uid, ...}
        self._removes: set[str] = set()          # removed uids
        self._entries: dict[str, ORSetEntry] = {}  # uid → metadata

    def add(self, element: str, *, node_id: str, ts: float | None = None) -> str:
        uid = str(uuid.uuid4())
        entry = ORSetEntry(uid=uid, node_id=node_id, ts=ts if ts is not None else time.time())
        self._adds.setdefault(element, set()).add(uid)
        self._entries[uid] = entry
        return uid

    def remove(self, element: str) -> None:
        for uid in self._adds.get(element, set()):
            self._removes.add(uid)

    def contains(self, element: str) -> bool:
        live = self._adds.get(element, set()) - self._removes
        return bool(live)

    def elements(self) -> list[str]:
        result = []
        for elem, uids in self._adds.items():
            if uids - self._removes:
                result.append(elem)
        return sorted(result)

    def merge(self, other: "ORSet") -> None:
        for elem, uids in other._adds.items():
            self._adds.setdefault(elem, set()).update(uids)
        self._removes.update(other._removes)
        self._entries.update(other._entries)

    def to_dict(self) -> dict:
        return {
            "adds": {k: list(v) for k, v in self._adds.items()},
            "removes": list(self._removes),
            "entries": {
                uid: {"node_id": e.node_id, "ts": e.ts}
                for uid, e in self._entries.items()
            },
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ORSet":
        s = cls()
        s._adds = {k: set(v) for k, v in data.get("adds", {}).items()}
        s._removes = set(data.get("removes", []))
        s._entries = {
            uid: ORSetEntry(uid=uid, node_id=e["node_id"], ts=e["ts"])
            for uid, e in data.get("entries", {}).items()
        }
        return s


# ---------------------------------------------------------------------------
# CRDTDocument — combined namespace for Sera memory
# ---------------------------------------------------------------------------

class CRDTDocument:
    """Three CRDT namespaces: chunks (ORSet IDs), entities (LWW), relations (LWW)."""

    def __init__(self) -> None:
        self.chunks = ORSet()
        self.entities = LWWRegister()
        self.relations = LWWRegister()

    def merge(self, other: "CRDTDocument") -> None:
        self.chunks.merge(other.chunks)
        self.entities.merge(other.entities)
        self.relations.merge(other.relations)

    def to_dict(self) -> dict:
        return {
            "chunks": self.chunks.to_dict(),
            "entities": self.entities.to_dict(),
            "relations": self.relations.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CRDTDocument":
        doc = cls()
        doc.chunks = ORSet.from_dict(data.get("chunks", {}))
        doc.entities = LWWRegister.from_dict(data.get("entities", {}))
        doc.relations = LWWRegister.from_dict(data.get("relations", {}))
        return doc


def merge(local: CRDTDocument, remote: CRDTDocument) -> CRDTDocument:
    """Return a new document that is the deterministic merge of local and remote."""
    result = CRDTDocument()
    result.merge(local)
    result.merge(remote)
    return result


# ---------------------------------------------------------------------------
# RelayStub — serialise/deserialise for future P-95 WebSocket transport
# ---------------------------------------------------------------------------

class RelayStub:
    """In-process relay that serialises to JSON bytes. The live WebSocket
    transport is sera.sync.relay.RelayServer/RelayClient."""

    @staticmethod
    def encode(doc: CRDTDocument) -> bytes:
        return json.dumps(doc.to_dict()).encode()

    @staticmethod
    def decode(payload: bytes) -> CRDTDocument:
        return CRDTDocument.from_dict(json.loads(payload.decode()))

    def push(self, doc: CRDTDocument) -> bytes:
        """Simulate push — returns the encoded payload (real relay would send it)."""
        return self.encode(doc)

    def pull(self, payload: bytes) -> CRDTDocument:
        """Simulate pull — decode incoming payload."""
        return self.decode(payload)
