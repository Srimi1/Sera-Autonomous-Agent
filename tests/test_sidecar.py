"""Tests for sera.rpc.server — the self-supervising sidecar entrypoint.

Boots the real HTTP API behind boot_sidecar, proves /healthz and /v1/turn
work, and verifies the PID-file single-instance lock.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import AsyncIterator

import pytest

from sera.gateway.router import Router
from sera.llm.base import StreamChunk
from sera.rpc.server import (
    AlreadyRunning,
    acquire_pid_lock,
    boot_sidecar,
    build_default_router,
    release_pid_lock,
)


def _http(method: str, url: str, *, body: dict | None = None, headers: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8") or "{}")


class _StubLLM:
    name = "openai"
    context_budget = 32_000
    model = "stub"

    async def stream(self, messages, tools=None, system=None) -> AsyncIterator[StreamChunk]:
        yield StreamChunk(delta_text="sidecar reply")
        yield StreamChunk(finish_reason="stop")


# ---------------------------------------------------------------------------
# PID lock
# ---------------------------------------------------------------------------

class TestPidLock:
    def test_acquire_writes_pid(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "sidecar.pid"
        acquire_pid_lock(pid_file)
        assert pid_file.read_text().strip() == str(os.getpid())
        release_pid_lock(pid_file)

    def test_stale_pid_reclaimed(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "sidecar.pid"
        # A PID that is almost certainly dead.
        pid_file.write_text("999999")
        acquire_pid_lock(pid_file)   # should reclaim, not raise
        assert pid_file.read_text().strip() == str(os.getpid())
        release_pid_lock(pid_file)

    def test_live_other_instance_refused(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "sidecar.pid"
        # The parent (pytest) is alive and is not us-as-sidecar; simulate a
        # different live instance by writing the PID of a real live process
        # that isn't our own getpid()... use the parent process id.
        ppid = os.getppid()
        if ppid == os.getpid():
            pytest.skip("no distinct live PID available")
        pid_file.write_text(str(ppid))
        with pytest.raises(AlreadyRunning):
            acquire_pid_lock(pid_file)

    def test_release_only_removes_own(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "sidecar.pid"
        pid_file.write_text("999999")   # someone else's
        release_pid_lock(pid_file)      # must NOT remove a foreign lock
        assert pid_file.exists()


# ---------------------------------------------------------------------------
# Boot + serve
# ---------------------------------------------------------------------------

class TestBoot:
    def test_healthz_up(self) -> None:
        router = Router(llm_factory=lambda _p: _StubLLM())
        handle = boot_sidecar(host="127.0.0.1", port=0, signing_key="test", router=router, pid_lock=False)
        try:
            status, body = _http("GET", f"{handle.url}/healthz")
            assert status == 200
            assert body["ok"] is True
        finally:
            handle.stop()

    def test_turn_round_trip(self) -> None:
        router = Router(llm_factory=lambda _p: _StubLLM())
        handle = boot_sidecar(host="127.0.0.1", port=0, signing_key="test", router=router, pid_lock=False)
        try:
            token = handle.bearer.issue("cli", scopes=["turn"])
            status, body = _http(
                "POST", f"{handle.url}/v1/turn",
                body={"text": "hi"},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert status == 200
            assert body["ok"] is True
            assert body["text"] == "sidecar reply"
        finally:
            handle.stop()

    def test_openapi_served(self) -> None:
        router = Router(llm_factory=lambda _p: _StubLLM())
        handle = boot_sidecar(host="127.0.0.1", port=0, signing_key="test", router=router, pid_lock=False)
        try:
            status, spec = _http("GET", f"{handle.url}/openapi.json")
            assert status == 200
            assert spec["openapi"] == "3.1.0"
        finally:
            handle.stop()

    def test_boots_without_api_key(self) -> None:
        """Crash-only: the sidecar comes up healthy even with no LLM key.

        build_default_router uses a lazy factory; /healthz must answer before
        any provider is provisioned.
        """
        router = build_default_router(workspace="/tmp")
        handle = boot_sidecar(host="127.0.0.1", port=0, signing_key="test", router=router, pid_lock=False)
        try:
            status, body = _http("GET", f"{handle.url}/healthz")
            assert status == 200
        finally:
            handle.stop()
