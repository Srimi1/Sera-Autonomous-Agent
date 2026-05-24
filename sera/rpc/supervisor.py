"""Crash-only supervisor for the Sera sidecar.

OUTCLASS: OpenHuman puts sidecar supervision in the Rust shell — so its core
dies for good if the desktop app isn't there to restart it. Sera supervises
itself in Python, which means crash-only recovery works EVERYWHERE the core
runs: under the Tauri shell, under `sera serve`, headless on a server, in a
container. The shell becomes a thin spawner of an already-self-healing core.

Crash-only design (Candea & Fox, 2003): the supervised process holds no
critical in-memory state. Everything that matters — sessions, identity,
memory — is on disk in SQLite. So "stop" and "crash" are the same event, and
"start" is the only recovery path. The supervisor never tries to drain or
cleanly hand off state; it just respawns. Killing the core loses nothing.

Guards against the two failure modes a naive `while True: spawn()` hits:
  - Tight crash loop → exponential backoff with jitter between restarts.
  - Crash storm (something is fundamentally broken) → a circuit breaker trips
    after N restarts in a window, so we stop burning CPU and surface the fault
    instead of flapping forever.

The process abstraction is injectable (`ProcessHandle`) so the supervision
logic is testable without real subprocesses; `SubprocessHandle` is the real
`subprocess.Popen` wrapper used in production.
"""
from __future__ import annotations

import logging
import random
import signal
import subprocess
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Deque, Protocol, Sequence

log = logging.getLogger("sera.rpc.supervisor")


# ---------------------------------------------------------------------------
# Process abstraction
# ---------------------------------------------------------------------------

class ProcessHandle(Protocol):
    """Minimal handle over a supervised OS process."""

    @property
    def pid(self) -> int: ...

    def poll(self) -> int | None:
        """Return exit code if the process has exited, else None (alive)."""
        ...

    def terminate(self) -> None:
        """Request graceful stop (SIGTERM)."""
        ...

    def kill(self) -> None:
        """Force stop (SIGKILL)."""
        ...

    def wait(self, timeout: float | None = None) -> int:
        """Block until exit; return the exit code."""
        ...


class SubprocessHandle:
    """Real ProcessHandle backed by subprocess.Popen."""

    def __init__(self, cmd: Sequence[str], **popen_kwargs) -> None:
        self._proc = subprocess.Popen(list(cmd), **popen_kwargs)

    @property
    def pid(self) -> int:
        return self._proc.pid

    def poll(self) -> int | None:
        return self._proc.poll()

    def terminate(self) -> None:
        self._proc.terminate()

    def kill(self) -> None:
        self._proc.kill()

    def wait(self, timeout: float | None = None) -> int:
        return self._proc.wait(timeout=timeout)


# ---------------------------------------------------------------------------
# Restart policy
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RestartPolicy:
    backoff_base_s: float = 0.5
    backoff_factor: float = 2.0
    backoff_max_s: float = 30.0
    jitter_frac: float = 0.1           # ± up to 10% randomization
    storm_threshold: int = 5           # restarts...
    storm_window_s: float = 60.0       # ...within this window trips the breaker

    def delay_for(self, consecutive_failures: int, rng: Callable[[], float] = random.random) -> float:
        """Exponential backoff with additive jitter.

        consecutive_failures starts at 1 for the first restart.
        """
        n = max(1, consecutive_failures)
        raw = min(self.backoff_base_s * (self.backoff_factor ** (n - 1)), self.backoff_max_s)
        jitter = raw * self.jitter_frac * rng()
        return raw + jitter


class SupervisorState(str, Enum):
    STOPPED = "stopped"
    RUNNING = "running"
    BACKOFF = "backoff"
    CIRCUIT_OPEN = "circuit_open"   # too many crashes; gave up


# ---------------------------------------------------------------------------
# Decision (pure) — what to do when we observe the child's status
# ---------------------------------------------------------------------------

class Action(str, Enum):
    KEEP = "keep"             # child alive, do nothing
    RESTART = "restart"       # child dead, respawn after delay
    CIRCUIT_BREAK = "circuit_break"   # too many crashes; stop


@dataclass(frozen=True)
class Decision:
    action: Action
    delay_s: float = 0.0
    reason: str = ""


# ---------------------------------------------------------------------------
# Supervisor
# ---------------------------------------------------------------------------

@dataclass
class Supervisor:
    """Spawns and crash-restarts a single child process.

    spawn:        () -> ProcessHandle. Called once per (re)start.
    policy:       backoff + circuit-breaker tuning.
    clock/sleep:  injectable time for deterministic tests.
    rng:          injectable jitter source.
    """

    spawn: Callable[[], ProcessHandle]
    policy: RestartPolicy = field(default_factory=RestartPolicy)
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    rng: Callable[[], float] = random.random

    def __post_init__(self) -> None:
        self._child: ProcessHandle | None = None
        self._state: SupervisorState = SupervisorState.STOPPED
        self._restart_count: int = 0
        self._consecutive_failures: int = 0
        self._stop_requested: bool = False
        self._restart_times: Deque[float] = deque()

    # -- introspection ------------------------------------------------------

    @property
    def state(self) -> SupervisorState:
        return self._state

    @property
    def restart_count(self) -> int:
        return self._restart_count

    @property
    def child(self) -> ProcessHandle | None:
        return self._child

    def is_alive(self) -> bool:
        return self._child is not None and self._child.poll() is None

    # -- lifecycle ----------------------------------------------------------

    def start_once(self) -> ProcessHandle:
        """Spawn the child and mark RUNNING."""
        self._child = self.spawn()
        self._state = SupervisorState.RUNNING
        log.info("supervisor: child started pid=%s", self._child.pid)
        return self._child

    def _record_restart(self, now: float) -> None:
        self._restart_times.append(now)
        cutoff = now - self.policy.storm_window_s
        while self._restart_times and self._restart_times[0] < cutoff:
            self._restart_times.popleft()

    def _storm_tripped(self) -> bool:
        return len(self._restart_times) >= self.policy.storm_threshold

    def decide(self, now: float) -> Decision:
        """Pure-ish decision: inspect the child, decide the next action.

        Does not spawn or sleep — `run` applies the decision. Separated so the
        restart/backoff/circuit logic is unit-testable.
        """
        if self._stop_requested:
            return Decision(Action.CIRCUIT_BREAK, reason="stop requested")
        if self._child is not None and self._child.poll() is None:
            return Decision(Action.KEEP, reason="child alive")

        # Child is dead (or never started). Check the storm window first.
        if self._storm_tripped():
            return Decision(
                Action.CIRCUIT_BREAK,
                reason=f"{len(self._restart_times)} restarts within "
                       f"{self.policy.storm_window_s:.0f}s — circuit open",
            )
        delay = self.policy.delay_for(self._consecutive_failures + 1, self.rng)
        return Decision(Action.RESTART, delay_s=delay, reason="child dead")

    def run(self, *, poll_interval_s: float = 0.2, max_steps: int | None = None) -> SupervisorState:
        """Supervise until stop() or the circuit trips.

        max_steps caps the loop (tests). None = run until terminal state.
        """
        if self._child is None and not self._stop_requested:
            self.start_once()

        steps = 0
        while True:
            if max_steps is not None and steps >= max_steps:
                return self._state
            steps += 1

            now = self.clock()
            decision = self.decide(now)

            if decision.action is Action.KEEP:
                self._state = SupervisorState.RUNNING
                self.sleep(poll_interval_s)
                continue

            if decision.action is Action.CIRCUIT_BREAK:
                if self._stop_requested:
                    self._state = SupervisorState.STOPPED
                    log.info("supervisor: stopped")
                else:
                    self._state = SupervisorState.CIRCUIT_OPEN
                    log.error("supervisor: circuit open — %s", decision.reason)
                return self._state

            # RESTART
            self._state = SupervisorState.BACKOFF
            self._consecutive_failures += 1
            log.warning(
                "supervisor: child dead (failure #%d), backing off %.2fs",
                self._consecutive_failures, decision.delay_s,
            )
            self.sleep(decision.delay_s)
            if self._stop_requested:
                self._state = SupervisorState.STOPPED
                return self._state
            self._record_restart(self.clock())
            self._restart_count += 1
            self.start_once()

    def mark_healthy(self) -> None:
        """Reset the consecutive-failure counter after a confirmed-good run.

        Call this once the child has been alive and serving past a grace
        window, so a later isolated crash starts backoff from the bottom
        instead of inheriting an old escalation.
        """
        self._consecutive_failures = 0

    def stop(self, *, term_grace_s: float = 3.0) -> None:
        """Request stop and terminate the child (SIGTERM → SIGKILL)."""
        self._stop_requested = True
        child = self._child
        if child is None:
            self._state = SupervisorState.STOPPED
            return
        if child.poll() is None:
            try:
                child.terminate()
                child.wait(timeout=term_grace_s)
            except Exception:  # noqa: BLE001 — escalate to SIGKILL
                with _suppress():
                    child.kill()
        self._state = SupervisorState.STOPPED
        log.info("supervisor: child stopped")


class _suppress:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc) -> bool:
        return True


# ---------------------------------------------------------------------------
# Convenience: supervise a shell command (production path)
# ---------------------------------------------------------------------------

def supervise_command(
    cmd: Sequence[str],
    *,
    policy: RestartPolicy | None = None,
    install_signals: bool = True,
    **popen_kwargs,
) -> Supervisor:
    """Build a Supervisor that spawns `cmd` via subprocess.

    With install_signals, SIGINT/SIGTERM to the supervisor trigger a clean
    `stop()` so the child is terminated before the supervisor exits.
    """
    def _spawn() -> ProcessHandle:
        return SubprocessHandle(cmd, **popen_kwargs)

    sup = Supervisor(spawn=_spawn, policy=policy or RestartPolicy())

    if install_signals:
        def _handler(signum, frame):  # noqa: ANN001, ARG001
            log.info("supervisor: signal %s → stopping child", signum)
            sup.stop()
        with _suppress():
            signal.signal(signal.SIGTERM, _handler)
            signal.signal(signal.SIGINT, _handler)

    return sup
