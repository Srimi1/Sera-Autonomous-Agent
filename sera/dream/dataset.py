"""Synthetic trace dataset exporter — P-72.

OUTCLASS: mlx-lm / unsloth compatible JSONL, validated before write.
Each record is a ChatML messages object — the canonical format both
frameworks accept without config shims.  A second pass deduplicates by
content-hash so repeated fine-tuning noise cannot accumulate.

Exported sources
----------------
1. SyntheticQA pairs from DreamEntry  →  user/assistant turn pairs
2. Session traces (if sessions provided directly)  →  instruction turns

Format (one JSON object per line)
----------------------------------
{"messages": [{"role": "user",      "content": "<question>"},
              {"role": "assistant", "content": "<answer>"}],
 "_meta": {"date": "2026-05-24", "source": "qa", "hash": "<sha8>"}}

The `_meta` key is ignored by mlx-lm and unsloth during training
but kept for traceability.  Remove it with `strip_meta=True` if you
need a pristine corpus for a third-party tool.
"""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Iterator

from sera.dream.journal import DreamEntry, DreamJournalStore, SyntheticQA

log = logging.getLogger("sera.dream.dataset")

# ---------------------------------------------------------------------------
# Record builders
# ---------------------------------------------------------------------------

def _sha8(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:8]


def _qa_record(qa: SyntheticQA, date: str, *, strip_meta: bool = False) -> dict[str, Any]:
    q = qa.question.strip()
    a = qa.answer.strip()
    rec: dict[str, Any] = {
        "messages": [
            {"role": "user",      "content": q},
            {"role": "assistant", "content": a},
        ],
    }
    if not strip_meta:
        rec["_meta"] = {
            "date": date,
            "source": "qa",
            "hash": _sha8(q + "\x00" + a),
            **({"session": qa.source_session_id} if qa.source_session_id else {}),
        }
    return rec


def _validate_record(rec: dict[str, Any]) -> bool:
    """Return True iff the record is a valid ChatML training example."""
    msgs = rec.get("messages")
    if not isinstance(msgs, list) or len(msgs) < 2:
        return False
    for m in msgs:
        if not isinstance(m, dict):
            return False
        if m.get("role") not in ("system", "user", "assistant"):
            return False
        if not isinstance(m.get("content"), str) or not m["content"].strip():
            return False
    return True


# ---------------------------------------------------------------------------
# Exporter
# ---------------------------------------------------------------------------

class DatasetExporter:
    """Reads a DreamJournalStore and writes mlx-lm / unsloth JSONL."""

    def __init__(self, store: DreamJournalStore) -> None:
        self._store = store

    # ------------------------------------------------------------------
    # Iteration
    # ------------------------------------------------------------------

    def iter_records(
        self,
        *,
        limit: int = 0,
        strip_meta: bool = False,
    ) -> Iterator[dict[str, Any]]:
        """Yield validated, deduplicated ChatML records from stored dream entries.

        Parameters
        ----------
        limit:
            Max number of dream entries to scan (0 = all).
        strip_meta:
            Drop the ``_meta`` key (for tools that reject unknown keys).
        """
        entries: list[DreamEntry] = (
            self._store.recent(limit=limit) if limit else self._store.recent(limit=10_000)
        )
        seen: set[str] = set()
        for entry in entries:
            for qa in entry.synthetic_qa:
                q = qa.question.strip()
                a = qa.answer.strip()
                if not q or not a:
                    continue
                fingerprint = _sha8(q + "\x00" + a)
                if fingerprint in seen:
                    continue
                seen.add(fingerprint)
                rec = _qa_record(qa, entry.date, strip_meta=strip_meta)
                if _validate_record(rec):
                    yield rec

    def count(self, *, limit: int = 0) -> int:
        return sum(1 for _ in self.iter_records(limit=limit))

    # ------------------------------------------------------------------
    # Export to file
    # ------------------------------------------------------------------

    def export(
        self,
        out: Path,
        *,
        limit: int = 0,
        strip_meta: bool = False,
    ) -> int:
        """Write JSONL to *out*; return number of records written.

        Writes atomically via a .tmp file so a partial export never
        leaves a corrupt corpus on disk.
        """
        tmp = out.with_suffix(".tmp")
        out.parent.mkdir(parents=True, exist_ok=True)
        n = 0
        with tmp.open("w", encoding="utf-8") as fh:
            for rec in self.iter_records(limit=limit, strip_meta=strip_meta):
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                n += 1
        tmp.replace(out)
        log.info("exported %d records → %s", n, out)
        return n

    # ------------------------------------------------------------------
    # Validation (read-back check)
    # ------------------------------------------------------------------

    @staticmethod
    def validate_file(path: Path) -> tuple[int, list[int]]:
        """Read *path* and return (valid_count, [bad_line_numbers]).

        A "bad" line is one that is not valid JSON, or whose messages
        block fails ``_validate_record``.
        """
        valid = 0
        bad: list[int] = []
        with path.open("r", encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    if _validate_record(rec):
                        valid += 1
                    else:
                        bad.append(lineno)
                except json.JSONDecodeError:
                    bad.append(lineno)
        return valid, bad
