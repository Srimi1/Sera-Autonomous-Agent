"""Tests for sera.dream.dataset — P-72 Synthetic Trace Dataset.

Phase verification: ≥100 valid (prompt, completion) pairs can be
produced and validated from a populated DreamJournalStore.
No network, no real model — stub entries directly.
"""
from __future__ import annotations

import json
from pathlib import Path


from sera.dream.dataset import DatasetExporter, _validate_record
from sera.dream.journal import DreamEntry, DreamJournalStore, SyntheticQA


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _store(tmp_path: Path) -> DreamJournalStore:
    return DreamJournalStore(db=tmp_path / "dream.db")


def _entry(date: str, qa_pairs: list[tuple[str, str]]) -> DreamEntry:
    qa = tuple(SyntheticQA(question=q, answer=a) for q, a in qa_pairs)
    return DreamEntry(date=date, created_at=1.0, summary="test", synthetic_qa=qa)


def _populated_store(tmp_path: Path, n_days: int, pairs_per_day: int) -> DreamJournalStore:
    store = _store(tmp_path)
    for d in range(n_days):
        pairs = [
            (f"What is day {d} item {i}?", f"Answer for day {d} item {i}.")
            for i in range(pairs_per_day)
        ]
        store.save(_entry(f"2026-05-{d + 1:02d}", pairs))
    return store


# ---------------------------------------------------------------------------
# _validate_record
# ---------------------------------------------------------------------------

class TestValidateRecord:
    def test_valid_two_turn(self) -> None:
        rec = {"messages": [
            {"role": "user",      "content": "Hello?"},
            {"role": "assistant", "content": "Hi there."},
        ]}
        assert _validate_record(rec) is True

    def test_valid_with_system(self) -> None:
        rec = {"messages": [
            {"role": "system",    "content": "You are Sera."},
            {"role": "user",      "content": "Hello?"},
            {"role": "assistant", "content": "Hi."},
        ]}
        assert _validate_record(rec) is True

    def test_missing_messages_key(self) -> None:
        assert _validate_record({"text": "bad"}) is False

    def test_empty_messages(self) -> None:
        assert _validate_record({"messages": []}) is False

    def test_single_message(self) -> None:
        assert _validate_record({"messages": [{"role": "user", "content": "hi"}]}) is False

    def test_invalid_role(self) -> None:
        rec = {"messages": [
            {"role": "unknown", "content": "?"},
            {"role": "assistant", "content": "!"},
        ]}
        assert _validate_record(rec) is False

    def test_empty_content_rejected(self) -> None:
        rec = {"messages": [
            {"role": "user",      "content": ""},
            {"role": "assistant", "content": "answer"},
        ]}
        assert _validate_record(rec) is False

    def test_whitespace_content_rejected(self) -> None:
        rec = {"messages": [
            {"role": "user",      "content": "   "},
            {"role": "assistant", "content": "answer"},
        ]}
        assert _validate_record(rec) is False


# ---------------------------------------------------------------------------
# DatasetExporter.iter_records
# ---------------------------------------------------------------------------

class TestIterRecords:
    def test_empty_store_yields_nothing(self, tmp_path: Path) -> None:
        exp = DatasetExporter(_store(tmp_path))
        assert list(exp.iter_records()) == []

    def test_basic_record_shape(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.save(_entry("2026-05-01", [("What is Sera?", "An autonomous agent.")]))
        exp = DatasetExporter(store)
        recs = list(exp.iter_records())
        assert len(recs) == 1
        msgs = recs[0]["messages"]
        assert msgs[0]["role"] == "user"
        assert msgs[0]["content"] == "What is Sera?"
        assert msgs[1]["role"] == "assistant"
        assert msgs[1]["content"] == "An autonomous agent."

    def test_meta_included_by_default(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.save(_entry("2026-05-01", [("Q?", "A.")]))
        recs = list(DatasetExporter(store).iter_records())
        assert "_meta" in recs[0]
        assert recs[0]["_meta"]["date"] == "2026-05-01"
        assert recs[0]["_meta"]["source"] == "qa"
        assert "hash" in recs[0]["_meta"]

    def test_strip_meta_removes_key(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.save(_entry("2026-05-01", [("Q?", "A.")]))
        recs = list(DatasetExporter(store).iter_records(strip_meta=True))
        assert "_meta" not in recs[0]

    def test_deduplication(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        qa = [("Same question?", "Same answer.")]
        store.save(_entry("2026-05-01", qa))
        store.save(_entry("2026-05-02", qa))  # duplicate content, different date
        recs = list(DatasetExporter(store).iter_records())
        assert len(recs) == 1, "duplicate (q,a) pairs must be deduplicated"

    def test_empty_question_skipped(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        entry = DreamEntry(
            date="2026-05-01", created_at=1.0, summary="s",
            synthetic_qa=(
                SyntheticQA(question="",  answer="answer"),
                SyntheticQA(question="Q", answer="A"),
            ),
        )
        store.save(entry)
        recs = list(DatasetExporter(store).iter_records())
        assert len(recs) == 1
        assert recs[0]["messages"][0]["content"] == "Q"

    def test_empty_answer_skipped(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        entry = DreamEntry(
            date="2026-05-01", created_at=1.0, summary="s",
            synthetic_qa=(
                SyntheticQA(question="Q", answer=""),
                SyntheticQA(question="Q2", answer="A2"),
            ),
        )
        store.save(entry)
        recs = list(DatasetExporter(store).iter_records())
        assert len(recs) == 1
        assert recs[0]["messages"][1]["content"] == "A2"

    def test_all_records_pass_validate(self, tmp_path: Path) -> None:
        store = _populated_store(tmp_path, n_days=5, pairs_per_day=7)
        exp = DatasetExporter(store)
        for rec in exp.iter_records():
            assert _validate_record(rec), f"invalid record: {rec}"

    def test_count(self, tmp_path: Path) -> None:
        store = _populated_store(tmp_path, n_days=3, pairs_per_day=4)
        assert DatasetExporter(store).count() == 12


# ---------------------------------------------------------------------------
# DatasetExporter.export (file write)
# ---------------------------------------------------------------------------

class TestExport:
    def test_writes_valid_jsonl(self, tmp_path: Path) -> None:
        store = _populated_store(tmp_path, n_days=2, pairs_per_day=3)
        out = tmp_path / "corpus.jsonl"
        n = DatasetExporter(store).export(out)
        assert n == 6
        lines = out.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 6
        for line in lines:
            rec = json.loads(line)
            assert _validate_record(rec)

    def test_empty_store_writes_empty_file(self, tmp_path: Path) -> None:
        out = tmp_path / "empty.jsonl"
        n = DatasetExporter(_store(tmp_path)).export(out)
        assert n == 0
        assert out.exists()
        assert out.read_text().strip() == ""

    def test_creates_parent_dirs(self, tmp_path: Path) -> None:
        store = _populated_store(tmp_path, n_days=1, pairs_per_day=2)
        out = tmp_path / "nested" / "deep" / "corpus.jsonl"
        DatasetExporter(store).export(out)
        assert out.exists()

    def test_atomic_write_no_tmp_on_success(self, tmp_path: Path) -> None:
        store = _populated_store(tmp_path, n_days=1, pairs_per_day=1)
        out = tmp_path / "out.jsonl"
        DatasetExporter(store).export(out)
        assert not (tmp_path / "out.tmp").exists()

    def test_strip_meta_in_file(self, tmp_path: Path) -> None:
        store = _populated_store(tmp_path, n_days=1, pairs_per_day=1)
        out = tmp_path / "clean.jsonl"
        DatasetExporter(store).export(out, strip_meta=True)
        rec = json.loads(out.read_text().strip())
        assert "_meta" not in rec


# ---------------------------------------------------------------------------
# DatasetExporter.validate_file
# ---------------------------------------------------------------------------

class TestValidateFile:
    def test_all_good(self, tmp_path: Path) -> None:
        store = _populated_store(tmp_path, n_days=3, pairs_per_day=4)
        out = tmp_path / "corpus.jsonl"
        DatasetExporter(store).export(out)
        valid, bad = DatasetExporter.validate_file(out)
        assert valid == 12
        assert bad == []

    def test_bad_json_line_flagged(self, tmp_path: Path) -> None:
        out = tmp_path / "bad.jsonl"
        good = json.dumps({"messages": [
            {"role": "user", "content": "Q"},
            {"role": "assistant", "content": "A"},
        ]})
        out.write_text(good + "\nnot-json\n" + good + "\n", encoding="utf-8")
        valid, bad = DatasetExporter.validate_file(out)
        assert valid == 2
        assert 2 in bad

    def test_invalid_schema_flagged(self, tmp_path: Path) -> None:
        out = tmp_path / "bad.jsonl"
        invalid = json.dumps({"messages": [{"role": "user", "content": "only one turn"}]})
        out.write_text(invalid + "\n", encoding="utf-8")
        valid, bad = DatasetExporter.validate_file(out)
        assert valid == 0
        assert 1 in bad

    def test_blank_lines_ignored(self, tmp_path: Path) -> None:
        out = tmp_path / "spaced.jsonl"
        good = json.dumps({"messages": [
            {"role": "user", "content": "Q"},
            {"role": "assistant", "content": "A"},
        ]})
        out.write_text("\n" + good + "\n\n" + good + "\n", encoding="utf-8")
        valid, bad = DatasetExporter.validate_file(out)
        assert valid == 2
        assert bad == []


# ---------------------------------------------------------------------------
# THE VERIFICATION: ≥100 valid pairs exported and re-validated
# ---------------------------------------------------------------------------

class TestHundredPairsVerification:
    def test_hundred_pairs_valid(self, tmp_path: Path) -> None:
        """Phase gate: a week of 15 pairs/day → 105 unique records, all valid."""
        store = _populated_store(tmp_path, n_days=7, pairs_per_day=15)
        out = tmp_path / "week.jsonl"
        exp = DatasetExporter(store)

        n = exp.export(out)
        assert n >= 100, f"expected ≥100 records, got {n}"

        valid, bad = DatasetExporter.validate_file(out)
        assert valid >= 100, f"expected ≥100 valid records, got {valid}"
        assert bad == [], f"unexpected invalid lines: {bad}"
