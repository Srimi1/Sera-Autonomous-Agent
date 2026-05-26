"""P-89: chaos monkey — crash-only design verification."""
from __future__ import annotations

from pathlib import Path


from sera.eval.chaos import (
    ChaosMonkey,
    ChaosReport,
    ChaosResult,
    _chaos_conn_drop,
    _chaos_concurrent_writes,
    _chaos_recovery_idempotent,
    _chaos_schema_inject,
    _chaos_write_abort,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fresh_db(tmp_path: Path, name: str = "chaos.db") -> Path:
    return tmp_path / name


# ---------------------------------------------------------------------------
# ChaosResult / ChaosReport shapes
# ---------------------------------------------------------------------------

def test_report_all_survived_when_all_pass():
    r = ChaosReport(results=[
        ChaosResult("A", True, "ok"),
        ChaosResult("B", True, "ok"),
    ])
    assert r.all_survived


def test_report_failures_listed():
    r = ChaosReport(results=[
        ChaosResult("A", True, "ok"),
        ChaosResult("B", False, "boom"),
    ])
    assert not r.all_survived
    assert len(r.failures) == 1
    assert r.failures[0].subsystem == "B"


def test_report_summary_string():
    r = ChaosReport(results=[ChaosResult("A", True, "ok")])
    assert "1/1" in r.summary()


# ---------------------------------------------------------------------------
# Individual scenarios
# ---------------------------------------------------------------------------

def test_write_abort_flags_session(tmp_path: Path):
    result = _chaos_write_abort(_fresh_db(tmp_path, "wa.db"), seed=0)
    assert result.subsystem == "WRITE_ABORT"
    assert result.survived, result.detail


def test_conn_drop_heals(tmp_path: Path):
    result = _chaos_conn_drop(_fresh_db(tmp_path, "cd.db"), seed=0)
    assert result.subsystem == "CONN_DROP"
    assert result.survived, result.detail


def test_concurrent_writes_no_deadlock(tmp_path: Path):
    result = _chaos_concurrent_writes(_fresh_db(tmp_path, "cw.db"), seed=42)
    assert result.subsystem == "CONCURRENT_WRITES"
    assert result.survived, result.detail


def test_schema_inject_idempotent(tmp_path: Path):
    from sera.memory.session import Session
    db = _fresh_db(tmp_path, "si.db")
    Session.create(workspace=str(tmp_path), db_path=db).close()
    result = _chaos_schema_inject(db, seed=0)
    assert result.subsystem == "SCHEMA_INJECT"
    assert result.survived, result.detail


def test_recovery_idempotent(tmp_path: Path):
    result = _chaos_recovery_idempotent(_fresh_db(tmp_path, "ri.db"), seed=0)
    assert result.subsystem == "RECOVERY_IDEMPOTENT"
    assert result.survived, result.detail


# ---------------------------------------------------------------------------
# ChaosMonkey full run
# ---------------------------------------------------------------------------

def test_chaos_monkey_full_run_all_survive(tmp_path: Path):
    report = ChaosMonkey(seed=42).run(_fresh_db(tmp_path))
    failures = [(r.subsystem, r.detail) for r in report.failures]
    assert report.all_survived, f"Chaos failures: {failures}"


def test_chaos_monkey_returns_five_results(tmp_path: Path):
    report = ChaosMonkey(seed=0).run(_fresh_db(tmp_path))
    assert len(report.results) == 5


def test_chaos_monkey_different_seeds_consistent(tmp_path: Path):
    for seed in (0, 1, 99, 2024):
        db = tmp_path / f"seed_{seed}.db"
        report = ChaosMonkey(seed=seed).run(db)
        assert report.all_survived, f"seed={seed} failures: {report.failures}"


# ---------------------------------------------------------------------------
# run_subset
# ---------------------------------------------------------------------------

def test_run_subset_filters_by_name(tmp_path: Path):
    report = ChaosMonkey(seed=0).run_subset(
        _fresh_db(tmp_path, "sub.db"), names=["WRITE_ABORT"]
    )
    names = [r.subsystem for r in report.results]
    assert "WRITE_ABORT" in names
    # Other scenarios not included
    assert all(r.subsystem == "WRITE_ABORT" for r in report.results)


def test_run_subset_empty_names_runs_nothing(tmp_path: Path):
    report = ChaosMonkey(seed=0).run_subset(
        _fresh_db(tmp_path, "empty.db"), names=["NOSUCHSUBSYSTEM"]
    )
    assert len(report.results) == 0


# ---------------------------------------------------------------------------
# Data integrity after chaos
# ---------------------------------------------------------------------------

def test_database_readable_after_all_chaos(tmp_path: Path):
    """After the full chaos run, the DB must still be queryable."""
    from sera.memory.session import Session, Message
    db = _fresh_db(tmp_path)
    ChaosMonkey(seed=7).run(db)
    # Must be able to create + write + read a new clean session
    s = Session.create(workspace=str(tmp_path), db_path=db)
    s.append(Message(role="user", content="post-chaos", finish_reason=None))
    s.append(Message(role="assistant", content="still alive", finish_reason="stop"))
    s.close()
    loaded = Session.load(s.id, db_path=db)
    assert loaded is not None
    assert len(loaded.messages) == 2
    loaded.close()
