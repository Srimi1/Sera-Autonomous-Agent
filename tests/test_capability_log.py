"""Tests for sera.dream.capability_log — P-76 Capability emergence tracker."""
from __future__ import annotations

from pathlib import Path


from sera.dream.capability_log import CapabilityLog


def _log(tmp_path: Path) -> CapabilityLog:
    t = [0.0]
    log = CapabilityLog(db=tmp_path / "cap.db", clock=lambda: t[0])
    log._t = t
    return log


class TestCapabilityLog:
    def test_record_snapshot_new_tools(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        n = log.record_snapshot("2026-05-20", tools=["web_search", "shell_run"], skills=[])
        assert n == 2
        assert log.count("tool") == 2

    def test_record_snapshot_new_skills(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        log.record_snapshot("2026-05-20", tools=[], skills=["deploy_digest"])
        assert log.count("skill") == 1

    def test_idempotent_same_name(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        log.record_snapshot("d1", tools=["web_search"], skills=[])
        n = log.record_snapshot("d2", tools=["web_search"], skills=[])
        assert n == 0, "second snapshot of same tool must not add a new entry"
        assert log.count("tool") == 1

    def test_first_seen_is_first_date(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        log._t[0] = 1.0
        log.record_snapshot("2026-05-20", tools=["web_search"], skills=[])
        log._t[0] = 2.0
        log.record_snapshot("2026-05-21", tools=["web_search"], skills=[])
        entry = log.timeline()[0]
        assert entry.first_seen == "2026-05-20"

    def test_timeline_oldest_first(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        for i, tool in enumerate(["a", "b", "c"]):
            log._t[0] = float(i)
            log.record_snapshot(f"d{i}", tools=[tool], skills=[])
        names = [e.name for e in log.timeline()]
        assert names == ["a", "b", "c"]

    def test_timeline_filter_by_kind(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        log.record_snapshot("d1", tools=["web_search"], skills=["deploy"])
        tools = log.timeline(kind="tool")
        skills = log.timeline(kind="skill")
        assert all(e.kind == "tool"  for e in tools)
        assert all(e.kind == "skill" for e in skills)

    def test_empty_log_returns_empty_timeline(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        assert log.timeline() == []

    def test_count_all(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        log.record_snapshot("d1", tools=["a", "b"], skills=["s1"])
        assert log.count() == 3

    def test_multi_night_accumulates(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        log._t[0] = 0.0
        log.record_snapshot("d1", tools=["web_search"], skills=[])
        log._t[0] = 1.0
        log.record_snapshot("d2", tools=["shell_run"], skills=["digest"])
        assert log.count() == 3

    def test_returns_count_of_new_only(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        log.record_snapshot("d1", tools=["a", "b"], skills=[])
        n = log.record_snapshot("d2", tools=["a", "c"], skills=[])
        assert n == 1   # only "c" is new
