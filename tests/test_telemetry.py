"""Tests for sera.telemetry.local — P-86 Local-only telemetry."""
from __future__ import annotations

import socket
from pathlib import Path

import pytest

from sera.telemetry.local import LocalTelemetry, TelemetryEvent, EventSummary


def _tel(tmp_path: Path) -> LocalTelemetry:
    t = [0.0]
    tel = LocalTelemetry(db=tmp_path / "tel.db", clock=lambda: t[0])
    tel._t = t
    return tel


class TestLocalTelemetry:
    def test_record_and_count(self, tmp_path: Path) -> None:
        tel = _tel(tmp_path)
        tel.record("tool_call", {"tool": "web_search"})
        assert tel.count() == 1

    def test_record_no_data(self, tmp_path: Path) -> None:
        tel = _tel(tmp_path)
        tel.record("session_start")
        assert tel.count() == 1

    def test_query_returns_events(self, tmp_path: Path) -> None:
        tel = _tel(tmp_path)
        tel.record("evt", {"x": 1})
        events = tel.query()
        assert len(events) == 1
        assert events[0].event == "evt"
        assert events[0].data == {"x": 1}

    def test_query_filter_by_event(self, tmp_path: Path) -> None:
        tel = _tel(tmp_path)
        tel.record("a", {})
        tel.record("b", {})
        tel.record("a", {})
        assert tel.count("a") == 2
        assert tel.count("b") == 1
        evts = tel.query(event="a")
        assert all(e.event == "a" for e in evts)

    def test_query_limit(self, tmp_path: Path) -> None:
        tel = _tel(tmp_path)
        for i in range(10):
            tel.record("e", {"i": i})
        assert len(tel.query(limit=3)) == 3

    def test_dashboard_groups_by_kind(self, tmp_path: Path) -> None:
        tel = _tel(tmp_path)
        for _ in range(5):
            tel.record("tool_call")
        for _ in range(3):
            tel.record("approval")
        summaries = tel.dashboard()
        kinds = {s.event: s.count for s in summaries}
        assert kinds["tool_call"] == 5
        assert kinds["approval"] == 3

    def test_dashboard_ordered_by_count(self, tmp_path: Path) -> None:
        tel = _tel(tmp_path)
        tel.record("rare")
        for _ in range(10):
            tel.record("frequent")
        summaries = tel.dashboard()
        assert summaries[0].event == "frequent"

    def test_data_preserved(self, tmp_path: Path) -> None:
        tel = _tel(tmp_path)
        tel.record("model_call", {"model": "claude-sonnet-4-6", "tokens": 1234})
        events = tel.query()
        assert events[0].data["tokens"] == 1234

    def test_empty_dashboard(self, tmp_path: Path) -> None:
        tel = _tel(tmp_path)
        assert tel.dashboard() == []


# ---------------------------------------------------------------------------
# THE VERIFICATION: zero network calls
# ---------------------------------------------------------------------------

class TestZeroNetworkCalls:
    def test_record_makes_no_network_calls(self, tmp_path: Path) -> None:
        """Phase gate: record() never touches the network."""
        network_calls: list[tuple] = []
        original_connect = socket.socket.connect

        def tracking_connect(self_sock, address):
            network_calls.append(address)
            return original_connect(self_sock, address)

        socket.socket.connect = tracking_connect
        try:
            tel = LocalTelemetry(db=tmp_path / "tel.db")
            for _ in range(5):
                tel.record("event", {"k": "v"})
            _ = tel.dashboard()
            _ = tel.query()
        finally:
            socket.socket.connect = original_connect

        assert network_calls == [], (
            f"LocalTelemetry made {len(network_calls)} network call(s): {network_calls}"
        )

    def test_no_network_imports(self) -> None:
        """telemetry/local.py must not import any network library."""
        import importlib, inspect
        import sera.telemetry.local as mod
        src = inspect.getsource(mod)
        bad_imports = ["import requests", "import httpx", "import urllib.request",
                       "import aiohttp", "import boto", "import google.cloud"]
        for bad in bad_imports:
            assert bad not in src, f"Found network import in local.py: {bad!r}"
