"""P-14: VaultSync bidirectional markdown sync + mtime watcher."""
from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

import pytest
import yaml

from sera.memory.embedder import StubEmbedder
from sera.memory.tree import MemoryTree
from sera.memory.vault import (
    IngestResult,
    VaultSync,
    VaultWatcher,
    render_file,
    split_frontmatter,
)


DIM = 16


def _run(coro):
    return asyncio.run(coro)


def _tree(tmp_path: Path) -> MemoryTree:
    return MemoryTree(db_path=tmp_path / "mem.db", embedding_dim=DIM)


def _vault(tmp_path: Path, *, embedder=None) -> tuple[MemoryTree, VaultSync]:
    tree = _tree(tmp_path)
    sync = VaultSync(tree=tree, vault_dir=tmp_path / "vault", embedder=embedder)
    return tree, sync


# ─── Frontmatter ─────────────────────────────────────────────────


def test_split_frontmatter_round_trip():
    raw = "---\nid: 7\nsource: notes\n---\n\nbody text\n"
    meta, body = split_frontmatter(raw)
    assert meta == {"id": 7, "source": "notes"}
    assert body.strip() == "body text"


def test_split_frontmatter_missing_returns_empty_meta():
    raw = "no frontmatter here\njust body\n"
    meta, body = split_frontmatter(raw)
    assert meta == {}
    assert body == raw


def test_render_file_inverses_split():
    meta = {"id": 1, "source": "s", "confidence": 0.9}
    body = "hello\n\nworld"
    rendered = render_file(meta, body)
    meta2, body2 = split_frontmatter(rendered)
    assert meta2 == meta
    assert body2.strip() == body


# ─── DB → vault ──────────────────────────────────────────────────


def test_write_chunk_creates_file_with_frontmatter(tmp_path: Path):
    tree, sync = _vault(tmp_path)
    cid = tree.add_chunk(source="notes", content="hello sera", confidence=0.8)
    path = sync.write_chunk(cid)
    assert path.exists()
    meta, body = split_frontmatter(path.read_text())
    assert meta["id"] == cid
    assert meta["source"] == "notes"
    assert meta["confidence"] == pytest.approx(0.8)
    assert body.strip() == "hello sera"


def test_export_all_writes_every_chunk(tmp_path: Path):
    tree, sync = _vault(tmp_path)
    for content in ("alpha", "beta", "gamma"):
        tree.add_chunk(source="s", content=content)
    paths = sync.export_all()
    assert len(paths) == 3
    for p in paths:
        assert p.exists()


def test_unsafe_source_is_sanitized(tmp_path: Path):
    tree, sync = _vault(tmp_path)
    cid = tree.add_chunk(source="../etc/passwd", content="nope")
    path = sync.write_chunk(cid)
    # Sanitization rewrites path components so the file stays inside the vault.
    assert sync.vault_dir in path.parents


# ─── Vault → DB ──────────────────────────────────────────────────


def test_ingest_file_inserts_when_no_id(tmp_path: Path):
    tree, sync = _vault(tmp_path)
    f = sync.vault_dir / "free" / "note.md"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("body without frontmatter\n")
    result = _run(sync.ingest_file(f))
    assert result.action == "inserted"
    chunk = tree.get_chunk(result.chunk_id)
    assert chunk is not None
    assert "body without frontmatter" in chunk.content
    # File is rewritten with the canonical id.
    meta, _ = split_frontmatter(f.read_text())
    assert meta["id"] == result.chunk_id


def test_ingest_file_updates_existing_id(tmp_path: Path):
    tree, sync = _vault(tmp_path)
    cid = tree.add_chunk(source="notes", content="original")
    path = sync.write_chunk(cid)
    # Edit the file body and re-ingest.
    meta, _ = split_frontmatter(path.read_text())
    path.write_text(render_file(meta, "rewritten"))
    result = _run(sync.ingest_file(path))
    assert result.action == "updated"
    assert result.chunk_id == cid
    chunk = tree.get_chunk(cid)
    assert "rewritten" in chunk.content


def test_ingest_file_with_stale_id_inserts_new(tmp_path: Path):
    """Frontmatter id pointing at a missing chunk → insert new + rewrite."""
    tree, sync = _vault(tmp_path)
    f = sync.vault_dir / "free" / "ghost.md"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("---\nid: 999999\nsource: free\n---\n\norphan body\n")
    result = _run(sync.ingest_file(f))
    assert result.action == "inserted"
    assert result.chunk_id != 999999
    meta, _ = split_frontmatter(f.read_text())
    assert meta["id"] == result.chunk_id


def test_ingest_file_embeds_when_embedder_present(tmp_path: Path):
    embedder = StubEmbedder(dim=DIM)
    tree, sync = _vault(tmp_path, embedder=embedder)
    f = sync.vault_dir / "src" / "note.md"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("---\nsource: src\n---\n\nfluffy cat on windowsill\n")
    result = _run(sync.ingest_file(f))
    # Search should now find the chunk via a related stub embedding.
    q = _run(embedder.embed("cat windowsill"))
    hits = tree.search(q, limit=1)
    assert hits and hits[0].chunk_id == result.chunk_id


def test_sync_from_disk_scans_all(tmp_path: Path):
    tree, sync = _vault(tmp_path)
    for i, content in enumerate(("alpha", "beta", "gamma")):
        f = sync.vault_dir / "s" / f"{i}.md"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content)
    results = _run(sync.sync_from_disk())
    assert len(results) == 3
    assert all(r.action == "inserted" for r in results)


# ─── Watcher ────────────────────────────────────────────────────


def test_watcher_fires_on_stable_change(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    fired: list[Path] = []
    event = threading.Event()

    def on_change(p):
        fired.append(p)
        event.set()

    w = VaultWatcher(vault, on_change, poll_interval=0.02)
    w.start()
    try:
        # Create + stabilize: write file, leave it alone for two polls.
        target = vault / "note.md"
        target.write_text("hello")
        # Wait long enough for at least two poll passes to see stable mtime.
        assert event.wait(timeout=2.0), "watcher never fired"
        assert target in fired
    finally:
        w.stop(timeout=1.0)


def test_watcher_stop_is_idempotent(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    w = VaultWatcher(vault, lambda _p: None, poll_interval=0.05)
    w.stop()  # never started — must not raise
    w.start()
    w.stop()
    w.stop()  # double stop — must not raise


def test_watcher_skips_unstable_writes(tmp_path: Path):
    """A file whose mtime keeps changing every poll should NOT fire."""
    vault = tmp_path / "vault"
    vault.mkdir()
    fired: list[Path] = []

    def on_change(p):
        fired.append(p)

    w = VaultWatcher(vault, on_change, poll_interval=0.03)
    target = vault / "wip.md"
    target.write_text("a")
    w.start()
    try:
        # Bump mtime every ~poll_interval so debounce never sees stability.
        end = time.monotonic() + 0.3
        while time.monotonic() < end:
            target.write_text(f"a {time.monotonic()}")
            time.sleep(0.01)
        assert not fired, "watcher fired despite unstable writes"
    finally:
        w.stop(timeout=1.0)


# ─── update / delete on tree (used by VaultSync) ────────────────


def test_update_chunk_partial_fields(tmp_path: Path):
    tree = _tree(tmp_path)
    cid = tree.add_chunk(source="s", content="v1", confidence=0.5)
    tree.update_chunk(cid, content="v2")
    assert tree.get_chunk(cid).content == "v2"
    assert tree.get_chunk(cid).confidence == pytest.approx(0.5)


def test_update_chunk_unknown_id_returns_false(tmp_path: Path):
    tree = _tree(tmp_path)
    assert tree.update_chunk(999, content="x") is False


def test_delete_chunk_removes_row(tmp_path: Path):
    tree = _tree(tmp_path)
    cid = tree.add_chunk(source="s", content="x", embedding=[0.0] * DIM)
    assert tree.delete_chunk(cid) is True
    assert tree.get_chunk(cid) is None
    assert tree.delete_chunk(cid) is False
