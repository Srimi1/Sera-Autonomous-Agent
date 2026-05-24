"""Sera sidecar entrypoint — the self-supervising core process.

`run_server` boots the P-59 HTTP API (Router + IdentityStore + SeraHTTPAPI) on
an asyncio loop, behind a PID-file single-instance lock, with a SIGTERM handler
that stops cleanly. This is the process the Tauri shell (or `sera serve`)
spawns, and the process the crash-only Supervisor restarts.

Crash-only: the server keeps nothing important in RAM. Sessions and identity
live in SQLite. A SIGKILL mid-request loses at most the in-flight turn; the
next process boots clean and every prior session is still on disk.

Boot is key-independent on purpose: the server comes up healthy even with no
LLM API key configured. /healthz answers immediately; only an actual /v1/turn
needs a provider key. A supervisor must be able to confirm liveness without
the agent being fully provisioned.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import threading
from dataclasses import dataclass
from pathlib import Path

from sera.config import SERA_HOME, load
from sera.gateway.identity import IdentityStore
from sera.gateway.router import Router
from sera.rpc.http_api import (
    SeraHTTPAPI,
    SignedBearer,
    make_async_bridge,
    make_streaming_bridge,
)

log = logging.getLogger("sera.rpc.server")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 11111            # matches the blueprint RPC port
PID_FILE = SERA_HOME / "sidecar.pid"


# ---------------------------------------------------------------------------
# Single-instance lock via PID file
# ---------------------------------------------------------------------------

def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True   # exists but owned by someone else
    return True


class AlreadyRunning(RuntimeError):
    def __init__(self, pid: int) -> None:
        super().__init__(f"sidecar already running (pid {pid})")
        self.pid = pid


def acquire_pid_lock(pid_file: Path = PID_FILE) -> None:
    """Refuse to start if a live sidecar already holds the PID file.

    A stale PID file (process gone) is reclaimed. This is the single-instance
    guarantee — two sidecars on one port would fight.
    """
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    if pid_file.exists():
        try:
            existing = int(pid_file.read_text().strip() or "0")
        except (ValueError, OSError):
            existing = 0
        if existing and existing != os.getpid() and _pid_alive(existing):
            raise AlreadyRunning(existing)
    pid_file.write_text(str(os.getpid()))


def release_pid_lock(pid_file: Path = PID_FILE) -> None:
    try:
        if pid_file.exists() and pid_file.read_text().strip() == str(os.getpid()):
            pid_file.unlink()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Router bootstrap
# ---------------------------------------------------------------------------

def build_default_router(*, workspace: str | None = None) -> Router:
    """Build a Router wired to the unified IdentityStore (P-60).

    The LLM factory is lazy: it resolves a provider only when a turn runs, so
    the server boots without an API key present.
    """
    cfg = load()
    identity = IdentityStore()
    ws = workspace or os.getcwd()

    def llm_factory(profile: str):
        from sera.llm.router import for_profile
        name = None if profile in ("default", "", None) else profile
        return for_profile(cfg, profile=name)

    return Router(llm_factory=llm_factory, session_resolver=identity.resolver(workspace=ws))


def _resolve_signing_key(explicit: str | None) -> str:
    if explicit:
        return explicit
    env = os.environ.get("SERA_API_KEY")
    if env:
        return env
    # Generate-and-persist a per-install key so tokens survive restarts.
    key_file = SERA_HOME / "api_signing_key"
    if key_file.exists():
        return key_file.read_text().strip()
    import secrets as _secrets
    key = _secrets.token_urlsafe(32)
    key_file.parent.mkdir(parents=True, exist_ok=True)
    key_file.write_text(key)
    try:
        os.chmod(key_file, 0o600)
    except OSError:
        pass
    return key


# ---------------------------------------------------------------------------
# Server handle
# ---------------------------------------------------------------------------

@dataclass
class SidecarHandle:
    api: SeraHTTPAPI
    loop: asyncio.AbstractEventLoop
    bearer: SignedBearer
    _loop_thread: threading.Thread

    @property
    def url(self) -> str:
        return self.api.url

    def stop(self) -> None:
        self.api.stop()
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._loop_thread.join(timeout=3.0)
        release_pid_lock()


def boot_sidecar(
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    signing_key: str | None = None,
    router: Router | None = None,
    pid_lock: bool = True,
) -> SidecarHandle:
    """Start the API + asyncio loop in a background thread. Non-blocking.

    Returns a handle whose .stop() tears everything down. Used by tests and by
    `run_server` (which then blocks on a stop Event).
    """
    if pid_lock:
        acquire_pid_lock()

    loop = asyncio.new_event_loop()
    loop_thread = threading.Thread(target=loop.run_forever, daemon=True, name="sera-sidecar-loop")
    loop_thread.start()

    rtr = router or build_default_router()
    bearer = SignedBearer(signing_key=_resolve_signing_key(signing_key))
    api = SeraHTTPAPI(
        host=host, port=port,
        turn_fn=make_async_bridge(loop, rtr),
        stream_fn=make_streaming_bridge(loop, rtr),
        bearer=bearer,
    )
    api.start()
    log.info("sidecar listening on %s", api.url)
    return SidecarHandle(api=api, loop=loop, bearer=bearer, _loop_thread=loop_thread)


def run_server(
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    signing_key: str | None = None,
) -> None:
    """Blocking entrypoint for `sera serve`. Runs until SIGTERM/SIGINT."""
    handle = boot_sidecar(host=host, port=port, signing_key=signing_key)
    stop_event = threading.Event()

    def _handler(signum, frame):  # noqa: ANN001, ARG001
        log.info("sidecar: signal %s → shutting down", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)

    try:
        stop_event.wait()
    finally:
        handle.stop()
