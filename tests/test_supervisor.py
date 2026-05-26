"""Tests for sera.rpc.supervisor — crash-only supervision.

P-61 outclass: kill the core, it restarts with backoff, sessions survive
because state is on disk. The supervision LOGIC is tested with injected fake
processes (deterministic); one real-subprocess test proves the spawn path
actually respawns an OS process; one test proves crash-only state survival.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from sera.memory.session import Message, Session
from sera.rpc.supervisor import (
    Action,
    RestartPolicy,
    Supervisor,
    SupervisorState,
    supervise_command,
)


# ---------------------------------------------------------------------------
# Fake process handle
# ---------------------------------------------------------------------------

class FakeProcess:
    """A ProcessHandle whose liveness we control.

    alive_for_polls=None → alive forever. 0 → dead on first poll.
    """

    _next_pid = 1000

    def __init__(self, *, alive_for_polls: int | None = None, exit_code: int = 0) -> None:
        FakeProcess._next_pid += 1
        self._pid = FakeProcess._next_pid
        self._alive_for = alive_for_polls
        self._exit_code = exit_code
        self._polls = 0
        self.terminated = False
        self.killed = False

    @property
    def pid(self) -> int:
        return self._pid

    def poll(self) -> int | None:
        self._polls += 1
        if self._alive_for is None:
            return None
        return self._exit_code if self._polls > self._alive_for else None

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout: float | None = None) -> int:
        return self._exit_code


def _recording_sleep():
    delays: list[float] = []

    def _sleep(d: float) -> None:
        delays.append(d)

    return _sleep, delays


def _fixed_clock(start: float = 0.0, step: float = 1.0):
    t = [start]

    def _clock() -> float:
        v = t[0]
        t[0] += step
        return v

    return _clock


# ---------------------------------------------------------------------------
# RestartPolicy
# ---------------------------------------------------------------------------

class TestRestartPolicy:
    def test_backoff_grows_exponentially(self) -> None:
        p = RestartPolicy(backoff_base_s=1.0, backoff_factor=2.0, backoff_max_s=100.0, jitter_frac=0.0)
        assert p.delay_for(1, rng=lambda: 0.0) == 1.0
        assert p.delay_for(2, rng=lambda: 0.0) == 2.0
        assert p.delay_for(3, rng=lambda: 0.0) == 4.0
        assert p.delay_for(4, rng=lambda: 0.0) == 8.0

    def test_backoff_capped(self) -> None:
        p = RestartPolicy(backoff_base_s=1.0, backoff_factor=10.0, backoff_max_s=5.0, jitter_frac=0.0)
        assert p.delay_for(10, rng=lambda: 0.0) == 5.0

    def test_jitter_adds_within_bound(self) -> None:
        p = RestartPolicy(backoff_base_s=10.0, backoff_factor=1.0, backoff_max_s=100.0, jitter_frac=0.1)
        # rng=1.0 → +10% of 10 = +1.0
        assert p.delay_for(1, rng=lambda: 1.0) == pytest.approx(11.0)


# ---------------------------------------------------------------------------
# Decision logic
# ---------------------------------------------------------------------------

class TestDecide:
    def test_alive_child_kept(self) -> None:
        sup = Supervisor(spawn=lambda: FakeProcess(alive_for_polls=None))
        sup.start_once()
        d = sup.decide(now=0.0)
        assert d.action is Action.KEEP

    def test_dead_child_restarts(self) -> None:
        sup = Supervisor(spawn=lambda: FakeProcess(alive_for_polls=0))
        sup.start_once()
        d = sup.decide(now=0.0)
        assert d.action is Action.RESTART
        assert d.delay_s > 0

    def test_stop_requested_breaks(self) -> None:
        sup = Supervisor(spawn=lambda: FakeProcess(alive_for_polls=None))
        sup.start_once()
        sup._stop_requested = True
        d = sup.decide(now=0.0)
        assert d.action is Action.CIRCUIT_BREAK


# ---------------------------------------------------------------------------
# Supervision loop with fakes
# ---------------------------------------------------------------------------

class TestSupervisionLoop:
    def test_healthy_child_never_restarts(self) -> None:
        sleep, delays = _recording_sleep()
        sup = Supervisor(spawn=lambda: FakeProcess(alive_for_polls=None), sleep=sleep)
        state = sup.run(max_steps=5)
        assert sup.restart_count == 0
        assert state is SupervisorState.RUNNING

    def test_dead_child_restarts_with_backoff(self) -> None:
        """Every spawned child dies → supervisor restarts until the storm trips."""
        sleep, delays = _recording_sleep()
        policy = RestartPolicy(
            backoff_base_s=1.0, backoff_factor=2.0, backoff_max_s=100.0,
            jitter_frac=0.0, storm_threshold=3, storm_window_s=1e9,
        )
        sup = Supervisor(
            spawn=lambda: FakeProcess(alive_for_polls=0),
            policy=policy, sleep=sleep, rng=lambda: 0.0,
        )
        state = sup.run()
        # 3 restarts then circuit opens.
        assert sup.restart_count == 3
        assert state is SupervisorState.CIRCUIT_OPEN
        # Backoff delays grew exponentially: 1, 2, 4.
        assert delays == [1.0, 2.0, 4.0]

    def test_circuit_breaks_on_storm(self) -> None:
        sleep, _ = _recording_sleep()
        policy = RestartPolicy(
            backoff_base_s=0.0, backoff_factor=1.0, backoff_max_s=0.0,
            jitter_frac=0.0, storm_threshold=2, storm_window_s=1e9,
        )
        sup = Supervisor(spawn=lambda: FakeProcess(alive_for_polls=0), policy=policy, sleep=sleep)
        state = sup.run()
        assert state is SupervisorState.CIRCUIT_OPEN
        assert sup.restart_count == 2

    def test_recovers_then_runs(self) -> None:
        """First spawn dies once, second spawn stays alive → settles RUNNING."""
        sleep, _ = _recording_sleep()
        procs = iter([FakeProcess(alive_for_polls=0), FakeProcess(alive_for_polls=None)])
        sup = Supervisor(
            spawn=lambda: next(procs),
            policy=RestartPolicy(backoff_base_s=0.0, jitter_frac=0.0, storm_threshold=5, storm_window_s=1e9),
            sleep=sleep,
        )
        state = sup.run(max_steps=6)
        assert sup.restart_count == 1
        assert state is SupervisorState.RUNNING

    def test_mark_healthy_resets_backoff(self) -> None:
        sup = Supervisor(spawn=lambda: FakeProcess(alive_for_polls=None))
        sup._consecutive_failures = 4
        sup.mark_healthy()
        assert sup._consecutive_failures == 0


# ---------------------------------------------------------------------------
# Stop / shutdown
# ---------------------------------------------------------------------------

class TestStop:
    def test_stop_terminates_child(self) -> None:
        proc = FakeProcess(alive_for_polls=None)
        sup = Supervisor(spawn=lambda: proc)
        sup.start_once()
        sup.stop()
        assert proc.terminated is True
        assert sup.state is SupervisorState.STOPPED

    def test_stop_before_start_is_clean(self) -> None:
        sup = Supervisor(spawn=lambda: FakeProcess(alive_for_polls=None))
        sup.stop()
        assert sup.state is SupervisorState.STOPPED

    def test_run_after_stop_exits_immediately(self) -> None:
        proc = FakeProcess(alive_for_polls=None)
        sup = Supervisor(spawn=lambda: proc)
        sup.start_once()
        sup.stop()
        state = sup.run(max_steps=3)
        assert state is SupervisorState.STOPPED


# ---------------------------------------------------------------------------
# Real subprocess — proves the spawn path actually respawns an OS process
# ---------------------------------------------------------------------------

class TestRealSubprocess:
    def test_real_process_respawns(self, tmp_path: Path) -> None:
        """Supervise a real python that appends a line then exits.

        Each (re)spawn appends one line; after the storm threshold the circuit
        opens. The line count proves real OS processes were respawned.
        """
        marker = tmp_path / "launches.txt"
        script = (
            f"open(r'{marker}', 'a').write('x\\n')"
        )
        cmd = [sys.executable, "-c", script]
        policy = RestartPolicy(
            backoff_base_s=0.01, backoff_factor=1.0, backoff_max_s=0.01,
            jitter_frac=0.0, storm_threshold=3, storm_window_s=1e9,
        )
        sup = supervise_command(cmd, policy=policy, install_signals=False)
        state = sup.run(poll_interval_s=0.01)

        assert state is SupervisorState.CIRCUIT_OPEN
        assert sup.restart_count == 3
        lines = marker.read_text().splitlines()
        # initial spawn + 3 restarts = 4 launches
        assert len(lines) == 4


# ---------------------------------------------------------------------------
# Crash-only state survival — the property the whole design rests on
# ---------------------------------------------------------------------------

class TestCrashOnlySurvival:
    def test_session_survives_process_death(self, tmp_path: Path) -> None:
        """State on disk survives a 'crash' — no in-memory handoff needed.

        Create a session (one 'process'), drop the object (simulate SIGKILL),
        then load it fresh (the restarted 'process'). It must be intact.
        """
        db = tmp_path / "sessions.db"
        s1 = Session.create(workspace=str(tmp_path), db_path=db)
        sid = s1.id
        s1.append(Message(role="user", content="remember: the vault code is 4271"))
        del s1  # simulate the process being killed

        s2 = Session.load(sid, db_path=db)
        assert s2 is not None
        assert s2.id == sid
        hits = s2.search("vault code", current_only=True)
        assert any("4271" in snip for _role, snip in hits)
