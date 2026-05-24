"""Tests for sera.safety.audit — P-84 Tamper-evident audit log."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from sera.safety.audit import AuditLog, AuditEntry, _entry_hash, _GENESIS_HASH


def _log(tmp_path: Path) -> AuditLog:
    t = [0.0]
    log = AuditLog(path=tmp_path / "audit.jsonl", clock=lambda: t[0])
    log._t = t
    return log


class TestAuditLog:
    def test_append_returns_entry(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        e = log.append("tool_call", {"tool": "shell_run", "args": {}})
        assert e.seq == 0
        assert e.kind == "tool_call"
        assert e.prev_hash == _GENESIS_HASH

    def test_second_entry_chains(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        e1 = log.append("session_start", {})
        e2 = log.append("tool_call", {"tool": "web_search"})
        assert e2.prev_hash == e1.hash

    def test_seq_monotonic(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        seqs = [log.append("x", {}).seq for _ in range(5)]
        assert seqs == list(range(5))

    def test_count(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        for i in range(4):
            log.append("event", {"i": i})
        assert log.count() == 4

    def test_entries_ordered(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        for i in range(3):
            log.append("e", {"i": i})
        entries = log.entries()
        assert [e.seq for e in entries] == [0, 1, 2]

    def test_verify_clean(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        for _ in range(5):
            log.append("evt", {})
        assert log.verify() == []

    def test_empty_log_verifies(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        assert log.verify() == []


class TestTamperDetection:
    def test_tampered_hash_detected(self, tmp_path: Path) -> None:
        log_path = tmp_path / "audit.jsonl"
        log = AuditLog(path=log_path)
        for i in range(3):
            log.append("e", {"i": i})

        # Tamper: rewrite line 1 with wrong hash
        lines = log_path.read_text().splitlines()
        d = json.loads(lines[1])
        d["payload"] = {"i": 999}         # corrupt payload
        lines[1] = json.dumps(d, separators=(",", ":"))
        log_path.write_text("\n".join(lines) + "\n")

        bad = log.verify()
        assert 1 in bad, f"tampered seq 1 must be flagged, got: {bad}"

    def test_tampered_middle_propagates(self, tmp_path: Path) -> None:
        log_path = tmp_path / "audit.jsonl"
        log = AuditLog(path=log_path)
        for i in range(5):
            log.append("e", {"i": i})

        # Tamper line 2 (seq=2)
        lines = log_path.read_text().splitlines()
        d = json.loads(lines[2])
        d["kind"] = "hacked"
        lines[2] = json.dumps(d, separators=(",", ":"))
        log_path.write_text("\n".join(lines) + "\n")

        bad = log.verify()
        # seq 2 is tampered; seq 3 also has broken prev_hash
        assert 2 in bad

    def test_first_entry_tampered(self, tmp_path: Path) -> None:
        log_path = tmp_path / "audit.jsonl"
        log = AuditLog(path=log_path)
        log.append("genesis", {})
        log.append("next", {})

        lines = log_path.read_text().splitlines()
        d = json.loads(lines[0])
        d["payload"] = {"injected": True}
        lines[0] = json.dumps(d, separators=(",", ":"))
        log_path.write_text("\n".join(lines) + "\n")

        bad = log.verify()
        assert 0 in bad

    def test_verify_returns_exact_line(self, tmp_path: Path) -> None:
        """Phase gate: edit one line → verify flags that exact seq number."""
        log_path = tmp_path / "audit.jsonl"
        log = AuditLog(path=log_path)
        for i in range(10):
            log.append("event", {"i": i})

        # Tamper seq=5 only
        lines = log_path.read_text().splitlines()
        d = json.loads(lines[5])
        d["payload"] = {"tampered": True}
        lines[5] = json.dumps(d, separators=(",", ":"))
        log_path.write_text("\n".join(lines) + "\n")

        bad = log.verify()
        assert 5 in bad, f"expected seq=5 in bad list, got {bad}"


class TestHashHelper:
    def test_deterministic(self) -> None:
        h1 = _entry_hash(0, 1.0, "x", {"k": "v"}, _GENESIS_HASH)
        h2 = _entry_hash(0, 1.0, "x", {"k": "v"}, _GENESIS_HASH)
        assert h1 == h2

    def test_changes_on_payload_change(self) -> None:
        h1 = _entry_hash(0, 1.0, "x", {"k": "v"}, _GENESIS_HASH)
        h2 = _entry_hash(0, 1.0, "x", {"k": "w"}, _GENESIS_HASH)
        assert h1 != h2

    def test_sha256_length(self) -> None:
        h = _entry_hash(0, 1.0, "e", {}, _GENESIS_HASH)
        assert len(h) == 64
