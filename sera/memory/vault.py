"""Bidirectional Obsidian-vault sync.

Each chunk in the `MemoryTree` is mirrored to a markdown file at
`<vault_dir>/<source>/<chunk-id>.md` with YAML frontmatter so the user
can open, edit, and reorganize chunks in Obsidian or any editor. A
lightweight mtime-poll watcher re-ingests changed files back into the
DB within ~2 seconds.

Outclass: OpenHuman mirrors one direction (DB → vault). Sera goes both
ways — the vault is the editing surface, the DB is the index. Edits in
the editor flow back into search results without the user touching the
CLI.
"""
from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

import yaml

from sera.config import SERA_HOME
from sera.memory.embedder import Embedder
from sera.memory.tree import MemoryTree

logger = logging.getLogger(__name__)

DEFAULT_VAULT_DIR = SERA_HOME / "vault"
FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_segment(name: str) -> str:
    """Sanitize a path segment so the filesystem accepts it."""
    cleaned = _SAFE_NAME.sub("-", (name or "default").strip()) or "default"
    return cleaned.strip("-.") or "default"


def split_frontmatter(text: str) -> tuple[dict, str]:
    """Split `---` YAML frontmatter from the body. Returns (meta, body).

    Missing frontmatter → empty dict + full text as body. Malformed YAML
    raises `yaml.YAMLError` upward so the caller can quarantine the file.
    """
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    meta = yaml.safe_load(m.group(1)) or {}
    if not isinstance(meta, dict):
        meta = {}
    body = text[m.end():]
    return meta, body


def render_file(meta: dict, body: str) -> str:
    """Inverse of `split_frontmatter` — render a complete markdown file."""
    front = yaml.safe_dump(meta, sort_keys=False).strip()
    return f"---\n{front}\n---\n\n{body.lstrip()}"


@dataclass(frozen=True)
class IngestResult:
    """Outcome of a single file's ingest pass."""

    path: Path
    chunk_id: int
    action: str  # "inserted" | "updated" | "skipped"


@dataclass
class VaultSync:
    """Mirror MemoryTree chunks to + from a markdown vault."""

    tree: MemoryTree
    vault_dir: Path = field(default_factory=lambda: DEFAULT_VAULT_DIR)
    embedder: Embedder | None = None

    def __post_init__(self) -> None:
        self.vault_dir = Path(self.vault_dir)
        self.vault_dir.mkdir(parents=True, exist_ok=True)

    # ─── DB → vault ──────────────────────────────────────────────

    def path_for(self, *, source: str, chunk_id: int) -> Path:
        return self.vault_dir / _safe_segment(source) / f"{chunk_id}.md"

    def write_chunk(self, chunk_id: int) -> Path:
        """Render a chunk to disk; overwrites any existing file.

        `consent=True` on the read: vault files are user-owned. The user
        opening their own `~/.sera/vault/...` markdown in an editor must
        see their own PII — auto-redacting their own files would be
        hostile. The consent gate sits between retrieval and the agent's
        working context, not between the DB and the user's editor.
        """
        chunk = self.tree.get_chunk(chunk_id, consent=True)
        if chunk is None:
            raise KeyError(f"chunk {chunk_id} not found")
        meta = {
            "id": chunk.id,
            "source": chunk.source,
            "summary": chunk.summary,
            "confidence": chunk.confidence,
            "created_at": chunk.created_at,
        }
        path = self.path_for(source=chunk.source, chunk_id=chunk.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_file(meta, chunk.content))
        return path

    def export_all(self) -> list[Path]:
        """Write every chunk to disk. Used on initial bootstrap of the vault."""
        rows = self.tree.conn.execute("SELECT id FROM chunks").fetchall()
        return [self.write_chunk(int(r[0])) for r in rows]

    # ─── Vault → DB ──────────────────────────────────────────────

    async def _embed_or_none(self, content: str) -> list[float] | None:
        if self.embedder is None:
            return None
        return await self.embedder.embed(content)

    async def ingest_file(self, path: Path) -> IngestResult:
        """Read a vault file and upsert its chunk into the tree.

        Behaviour:
          * Frontmatter `id` present + chunk exists in DB → update.
          * Frontmatter `id` present + chunk missing → insert a new chunk
            (sqlite-assigned id); the file is rewritten with the new id
            so it stays self-consistent.
          * No frontmatter `id` → insert new; rewrite file with id.

        Re-embeds the body when an `embedder` is configured.
        """
        text = path.read_text()
        meta, body = split_frontmatter(text)
        source = meta.get("source") or path.parent.name or "default"
        summary = meta.get("summary") or ""
        confidence = float(meta.get("confidence") or 1.0)
        embedding = await self._embed_or_none(body)

        existing_id = meta.get("id")
        if isinstance(existing_id, int):
            updated = self.tree.update_chunk(
                existing_id,
                content=body,
                summary=summary,
                confidence=confidence,
                embedding=embedding,
            )
            if updated:
                return IngestResult(path=path, chunk_id=existing_id, action="updated")

        new_id = self.tree.add_chunk(
            source=source,
            content=body,
            summary=summary,
            confidence=confidence,
            embedding=embedding,
        )
        # Rewrite the file with the canonical id so future polls don't
        # treat it as new.
        meta["id"] = new_id
        meta.setdefault("source", source)
        meta.setdefault("confidence", confidence)
        path.write_text(render_file(meta, body))
        return IngestResult(path=path, chunk_id=new_id, action="inserted")

    async def sync_from_disk(self) -> list[IngestResult]:
        """Walk the vault and re-ingest every markdown file.

        Skeleton: full re-scan. The watcher path (`VaultWatcher`) handles
        change detection for interactive use; this method is the "sync
        now" hammer.
        """
        results: list[IngestResult] = []
        for f in sorted(self.vault_dir.rglob("*.md")):
            results.append(await self.ingest_file(f))
        return results


# ─── Watcher ─────────────────────────────────────────────────────


class VaultWatcher:
    """Mtime-poll watcher with 2-poll stability debounce.

    Implementation deliberately avoids a `watchdog` dep — the polling
    interval defaults to 1.0s; tests run with a much shorter one. The
    callback fires exactly once per stabilized change (second poll sees
    the same mtime as the first).
    """

    def __init__(
        self,
        vault_dir: Path,
        on_change: Callable[[Path], None],
        *,
        poll_interval: float = 1.0,
    ) -> None:
        self.vault_dir = Path(vault_dir)
        self.on_change = on_change
        self.poll_interval = poll_interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # Per-file (mtime, last_seen_pass) tracking. We fire when a file's
        # mtime changes AND survives one extra poll unchanged — that's
        # the "debounce". Without it a partial editor write would re-fire.
        self._mtimes: dict[Path, float] = {}
        self._pending: dict[Path, float] = {}

    def _scan(self) -> Iterable[Path]:
        return sorted(self.vault_dir.rglob("*.md"))

    def _tick(self) -> list[Path]:
        """One poll pass. Returns the list of files that fired this tick."""
        fired: list[Path] = []
        seen: set[Path] = set()
        for f in self._scan():
            seen.add(f)
            try:
                mtime = f.stat().st_mtime
            except FileNotFoundError:
                continue
            prev = self._mtimes.get(f)
            pending = self._pending.get(f)
            if prev is None:
                # New file: schedule for next-pass confirmation.
                self._pending[f] = mtime
                continue
            if mtime != prev:
                self._pending[f] = mtime
                continue
            if pending is not None and mtime == pending:
                # Stable for two polls → fire.
                fired.append(f)
                self._mtimes[f] = mtime
                self._pending.pop(f, None)
        # Promote first-seen files whose mtime didn't change between the
        # discovery pass and this pass.
        for f, mtime in list(self._pending.items()):
            if f not in self._mtimes:
                self._mtimes[f] = mtime
        for path in fired:
            try:
                self.on_change(path)
            except Exception as e:  # noqa: BLE001 — callbacks are user code
                logger.exception("vault watcher callback failed for %s: %s", path, e)
        return fired

    def _run(self) -> None:
        while not self._stop.is_set():
            self._tick()
            self._stop.wait(self.poll_interval)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
