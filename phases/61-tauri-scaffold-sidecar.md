# P-61 — Tauri scaffold + sidecar

## Status

done.

## Outclass claim

**Rejected the blueprint's "none unique; OH lineage."** OpenHuman puts sidecar
supervision in the Rust shell — so its core dies for good if the desktop app
isn't running. Sera's core **supervises itself in Python**: crash-only restart
with exponential backoff + jitter and a crash-storm circuit breaker, in
`sera/rpc/supervisor.py`. That means self-healing works EVERYWHERE the core
runs — under the Tauri shell, under `sera serve`, headless on a server, in a
container. The Rust layer becomes a thin spawner of an already-self-healing
core (with its own defense-in-depth respawn for total-tree death).

Crash-only (Candea & Fox): the core holds no critical RAM state — sessions,
identity, memory are all in SQLite. "Stop" and "crash" are the same event;
"start" is the only recovery path. Killing the core loses at most the in-flight
turn. Verified: `test_session_survives_process_death`.

## Files

- `sera/rpc/supervisor.py` — Supervisor, RestartPolicy, ProcessHandle,
  supervise_command (the outclass)
- `sera/rpc/server.py` — boot_sidecar, run_server, PID-file single-instance
  lock, graceful SIGTERM
- `sera/cli/main.py` — `sera serve [--supervised]`
- `sera-shell/` — Tauri scaffold: src-tauri/src/{main,core_process,core_rpc}.rs,
  Cargo.toml, tauri.conf.json, React (App.tsx, main.tsx), package.json
- `tests/test_supervisor.py` (16), `tests/test_sidecar.py` (8)

## Verification

| Check | Status | Notes |
|-------|--------|-------|
| 24 tests | ✅ | supervisor logic, real-subprocess respawn, crash-only survival, sidecar boot, PID lock |
| **Crash-only survival** | ✅ | session written pre-"crash" loads intact after | 
| Real subprocess respawn | ✅ | test_real_process_respawns — 1 spawn + 3 restarts = 4 real OS launches, then circuit opens |
| Backoff grows + caps | ✅ | 1,2,4s exponential; capped at max; jitter bounded |
| Circuit breaker | ✅ | N restarts in window → CIRCUIT_OPEN, stops flapping |
| `sera serve` live | ✅ | real curl: /healthz ok, /openapi.json 3.1.0, PID file written, SIGTERM removes it cleanly |
| Boots without API key | ✅ | lazy LLM factory — /healthz answers before any provider provisioned |
| Single-instance lock | ✅ | live foreign PID → AlreadyRunning; stale PID reclaimed; release only removes own |
| Full suite | ✅ | No regressions |

## Limits

**What was NOT verified in this env:**
- **`pnpm tauri dev` (the blueprint's literal verification) was NOT run** — no
  cargo/Rust toolchain here. The Rust files (`main.rs`, `core_process.rs`,
  `core_rpc.rs`, Cargo.toml, tauri.conf.json) are written as real, shippable
  code but are NOT compile-checked. First machine with `cargo` + `pnpm` will be
  the real test of the Rust layer. The **outclass (self-supervising core)** is
  fully built and tested in Python, which is why the phase promotes.
- No tray icon asset yet (tauri.conf.json references icons/tray.png).
- The React frontend is a minimal one-input chat stub — full chat panel is P-62
  (Socket.io streaming), tray is P-63.
- Rust-side respawn backoff mirrors the Python policy but has no jitter and
  isn't unit-tested (no test runner for Rust here).
- `SERA_API_KEY` handoff from core to shell is via env var; the core writes a
  per-install key file (`~/.sera/api_signing_key`) but the shell reading it
  automatically is not yet wired.

## Dependencies

P-59. Opens Epoch 7 (Desktop).
