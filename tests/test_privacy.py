"""P-19: privacy + redaction + search-with-consent."""
from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from sera.memory.embedder import StubEmbedder
from sera.memory.privacy import (
    PIIMatch,
    detect,
    has_pii,
    known_kinds,
    pii_kinds,
    redact_pii,
)
from sera.memory.search import hybrid_search
from sera.memory.tree import MemoryTree


DIM = 16


def _run(coro):
    return asyncio.run(coro)


def _tree(tmp_path: Path) -> MemoryTree:
    return MemoryTree(db_path=tmp_path / "mem.db", embedding_dim=DIM)


# ─── Detection per kind ────────────────────────────────────────


@pytest.mark.parametrize(
    "text,expected",
    [
        ("my ssn is 123-45-6789 ok", "ssn"),
        ("contact me at jane@example.com please", "email"),
        ("ping 192.168.1.42 on the lan", "ipv4"),
        ("call +1 415-555-1234 anytime", "phone"),
        ("key sk-ant-api01-" + "A" * 30, "anthropic_key"),
        ("token ghp_" + "B" * 30, "github_pat"),
        ("aws id AKIAIOSFODNN7EXAMPLE", "aws_access_key"),
    ],
)
def test_each_detector_fires(text, expected):
    kinds = pii_kinds(text)
    assert expected in kinds


def test_credit_card_requires_luhn_check():
    """Only Luhn-valid digit runs count as credit cards."""
    # Valid Visa test card (Luhn ok).
    valid = "4111 1111 1111 1111"
    # Same length, fails Luhn.
    invalid = "4111 1111 1111 1112"
    assert "credit_card" in pii_kinds(valid)
    assert "credit_card" not in pii_kinds(invalid)


def test_detect_returns_spans_sorted_and_non_overlapping():
    text = "contact jane@example.com or sk-ant-api01-" + "A" * 30
    matches = detect(text)
    assert all(isinstance(m, PIIMatch) for m in matches)
    assert matches == sorted(matches, key=lambda m: m.start)
    # Non-overlap: each match ends ≤ next starts.
    for a, b in zip(matches, matches[1:]):
        assert a.end <= b.start


def test_has_pii_short_circuits():
    assert has_pii("just normal text") is False
    assert has_pii("") is False
    assert has_pii("ssn 999-00-1111") is True


def test_redact_pii_replaces_inline():
    text = "email a@b.com and ssn 111-22-3333"
    out = redact_pii(text)
    assert "a@b.com" not in out
    assert "111-22-3333" not in out
    assert "<redacted:email>" in out
    assert "<redacted:ssn>" in out


def test_redact_pii_passthrough_when_clean():
    assert redact_pii("nothing sensitive here") == "nothing sensitive here"


def test_redact_pii_custom_marker():
    out = redact_pii("ssn 111-22-3333", marker="***")
    assert out == "ssn ***"


def test_pii_kinds_dedupes():
    text = "emails a@b.com and c@d.com plus more"
    kinds = pii_kinds(text)
    assert kinds.count("email") == 1


def test_known_kinds_is_finite():
    kinds = set(known_kinds())
    assert "ssn" in kinds and "email" in kinds and "credit_card" in kinds


# ─── Ingest persists tags ──────────────────────────────────────


def test_add_chunk_persists_pii_tags(tmp_path: Path):
    tree = _tree(tmp_path)
    cid = tree.add_chunk(
        source="s", content="contact: ops@acme.com, ssn 555-44-3210",
    )
    chunk = tree.get_chunk(cid)
    assert set(chunk.pii_tags) == {"email", "ssn"}


def test_add_chunk_clean_content_has_no_tags(tmp_path: Path):
    tree = _tree(tmp_path)
    cid = tree.add_chunk(source="s", content="just plain text")
    chunk = tree.get_chunk(cid)
    assert chunk.pii_tags == ()


def test_update_chunk_retags(tmp_path: Path):
    tree = _tree(tmp_path)
    cid = tree.add_chunk(source="s", content="boring stuff")
    assert tree.get_chunk(cid).pii_tags == ()
    tree.update_chunk(cid, content="now with ssn 123-45-6789")
    assert "ssn" in tree.get_chunk(cid).pii_tags


def test_update_chunk_clears_tags_when_pii_removed(tmp_path: Path):
    tree = _tree(tmp_path)
    cid = tree.add_chunk(source="s", content="email a@b.com")
    assert tree.get_chunk(cid).pii_tags == ("email",)
    tree.update_chunk(cid, content="cleaned up")
    assert tree.get_chunk(cid).pii_tags == ()


# ─── Consent gate on hybrid_search ─────────────────────────────


def test_hybrid_search_default_redacts(tmp_path: Path):
    tree = _tree(tmp_path)
    e = StubEmbedder(dim=DIM)
    secret_body = "the deploy key is sk-ant-api01-" + "Z" * 30
    cid = tree.add_chunk(
        source="s", content=secret_body, embedding=_run(e.embed(secret_body)),
    )
    hits = hybrid_search(
        tree, "deploy key", query_embedding=_run(e.embed("deploy key")), k=1,
        touch=False,
    )
    assert hits and hits[0].chunk_id == cid
    assert hits[0].redacted is True
    assert "sk-ant-api01" not in hits[0].content
    assert "anthropic_key" in hits[0].content
    assert "anthropic_key" in hits[0].pii_tags


def test_hybrid_search_consent_reveals(tmp_path: Path):
    tree = _tree(tmp_path)
    e = StubEmbedder(dim=DIM)
    body = "email contact: ops@acme.com"
    cid = tree.add_chunk(
        source="s", content=body, embedding=_run(e.embed(body)),
    )
    hits = hybrid_search(
        tree, "ops contact", query_embedding=_run(e.embed("ops contact")),
        k=1, consent=True, touch=False,
    )
    assert hits and hits[0].chunk_id == cid
    assert hits[0].redacted is False
    assert "ops@acme.com" in hits[0].content


def test_clean_chunk_never_redacted(tmp_path: Path):
    tree = _tree(tmp_path)
    e = StubEmbedder(dim=DIM)
    body = "just a fact about turbines"
    tree.add_chunk(source="s", content=body, embedding=_run(e.embed(body)))
    hits = hybrid_search(
        tree, "turbines", query_embedding=_run(e.embed("turbines")), k=1,
        touch=False,
    )
    assert hits[0].redacted is False
    assert hits[0].pii_tags == ()
    assert "turbines" in hits[0].content


# ─── Migration ─────────────────────────────────────────────────


def test_legacy_db_migration_adds_pii_tags(tmp_path: Path):
    db = tmp_path / "legacy.db"
    legacy = sqlite3.connect(db)
    legacy.executescript(
        """
        CREATE TABLE chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            content TEXT NOT NULL,
            summary TEXT,
            confidence REAL NOT NULL DEFAULT 1.0,
            embedding BLOB,
            created_at REAL NOT NULL
        );
        CREATE TABLE entities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            type TEXT NOT NULL,
            first_seen REAL NOT NULL,
            last_seen REAL NOT NULL
        );
        CREATE TABLE relations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            src_entity_id INTEGER NOT NULL,
            dst_entity_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            confidence REAL NOT NULL DEFAULT 1.0,
            provenance_chunk_id INTEGER,
            created_at REAL NOT NULL
        );
        """
    )
    legacy.commit()
    legacy.close()

    tree = MemoryTree(db_path=db, embedding_dim=DIM)
    cols = {r[1] for r in tree.conn.execute("PRAGMA table_info(chunks)").fetchall()}
    assert "pii_tags" in cols
