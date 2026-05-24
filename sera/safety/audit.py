"""Tamper-evident audit log — SHA-256 hash chain (P-84).

OUTCLASS: Every tool call, approval decision, and session event is chained:
each entry's hash covers the previous entry's hash.  Tamper one byte and
`sera audit verify` flags the exact line number.  Nobody else ships this.

Chain design
------------
- Entries stored as JSONL (one JSON object per line).
- Each entry: {seq, ts, kind, payload, prev_hash, hash}
- `prev_hash` for seq=0 is the genesis sentinel "0"*64.
- `hash` = SHA-256 of canonical JSON of all other fields.
- Verification walks the file, recomputes each hash, checks chain linkage.

The log is append-only.  Entries are never edited.  Rotation (archiving old
entries) is a future concern; for now the file grows and verification stays O(n).
"""
from __future__ import annotations

import hashlib
import json
import time as _time_mod
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from sera.config import SERA_HOME

AUDIT_LOG = SERA_HOME / "audit.jsonl"
_GENESIS_HASH = "0" * 64


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AuditEntry:
    seq: int
    ts: float
    kind: str
    payload: dict[str, Any]
    prev_hash: str
    hash: str


# ---------------------------------------------------------------------------
# Hash helper
# ---------------------------------------------------------------------------

def _entry_hash(seq: int, ts: float, kind: str, payload: dict, prev_hash: str) -> str:
    canon = json.dumps(
        {"seq": seq, "ts": ts, "kind": kind, "payload": payload, "prev_hash": prev_hash},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Log
# ---------------------------------------------------------------------------

class AuditLog:
    """Append-only, tamper-evident audit log with SHA-256 hash chain."""

    def __init__(
        self,
        path: Path | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._path = path or AUDIT_LOG
        self._clock = clock or _time_mod.time
        self._path.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def append(self, kind: str, payload: dict[str, Any]) -> AuditEntry:
        """Append one entry and return it."""
        prev = self._last_hash()
        seq  = self._next_seq()
        ts   = self._clock()
        h    = _entry_hash(seq, ts, kind, payload, prev)
        entry = AuditEntry(seq=seq, ts=ts, kind=kind, payload=payload,
                           prev_hash=prev, hash=h)
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(asdict(entry), separators=(",", ":")) + "\n")
        return entry

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def entries(self, limit: int = 0) -> list[AuditEntry]:
        if not self._path.exists():
            return []
        result: list[AuditEntry] = []
        with self._path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                d = json.loads(line)
                result.append(AuditEntry(
                    seq=d["seq"], ts=d["ts"], kind=d["kind"],
                    payload=d["payload"], prev_hash=d["prev_hash"], hash=d["hash"],
                ))
        if limit:
            return result[-limit:]
        return result

    def count(self) -> int:
        return len(self.entries())

    # ------------------------------------------------------------------
    # Verify
    # ------------------------------------------------------------------

    def verify(self) -> list[int]:
        """Walk the chain; return list of bad seq numbers (empty = all good)."""
        bad: list[int] = []
        prev_hash = _GENESIS_HASH
        for entry in self.entries():
            expected = _entry_hash(entry.seq, entry.ts, entry.kind,
                                   entry.payload, entry.prev_hash)
            if entry.hash != expected:
                bad.append(entry.seq)
            if entry.prev_hash != prev_hash:
                if entry.seq not in bad:
                    bad.append(entry.seq)
            prev_hash = entry.hash
        return sorted(set(bad))

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _last_hash(self) -> str:
        es = self.entries()
        return es[-1].hash if es else _GENESIS_HASH

    def _next_seq(self) -> int:
        es = self.entries()
        return es[-1].seq + 1 if es else 0
