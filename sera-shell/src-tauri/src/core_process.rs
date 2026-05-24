//! Sidecar lifecycle — spawn the Python core and keep it alive.
//!
//! The Python core (`sera serve`) already self-supervises (crash-only
//! restart with backoff, in `sera/rpc/supervisor.py`). So this Rust layer is
//! intentionally thin: it spawns the core once, holds the child handle, and
//! provides defense-in-depth — if the *whole* core tree dies (not just one
//! worker), the shell respawns it. State lives in SQLite, so a respawn loses
//! nothing.

use std::process::{Child, Command};
use std::sync::Mutex;
use std::time::{Duration, Instant};

/// Backoff bounds for the shell-level respawn (mirrors the Python policy).
const BACKOFF_BASE: Duration = Duration::from_millis(500);
const BACKOFF_MAX: Duration = Duration::from_secs(30);
const STORM_THRESHOLD: usize = 5;
const STORM_WINDOW: Duration = Duration::from_secs(60);

pub struct CoreProcess {
    child: Mutex<Option<Child>>,
    host: String,
    port: u16,
    restart_times: Mutex<Vec<Instant>>,
}

impl CoreProcess {
    pub fn new(host: impl Into<String>, port: u16) -> Self {
        Self {
            child: Mutex::new(None),
            host: host.into(),
            port,
            restart_times: Mutex::new(Vec::new()),
        }
    }

    /// Spawn `sera serve`. The core self-supervises internally; we run it
    /// plain here and let this layer catch only a total-tree death.
    pub fn spawn(&self) -> std::io::Result<u32> {
        let child = Command::new("sera")
            .arg("serve")
            .arg("--host")
            .arg(&self.host)
            .arg("--port")
            .arg(self.port.to_string())
            .spawn()?;
        let pid = child.id();
        *self.child.lock().unwrap() = Some(child);
        Ok(pid)
    }

    /// True if the core process is still running.
    pub fn is_alive(&self) -> bool {
        let mut guard = self.child.lock().unwrap();
        match guard.as_mut() {
            Some(c) => matches!(c.try_wait(), Ok(None)),
            None => false,
        }
    }

    /// Backoff delay for the Nth consecutive failure (1-based).
    fn backoff_for(&self, consecutive: u32) -> Duration {
        let n = consecutive.max(1);
        let raw = BACKOFF_BASE
            .checked_mul(2u32.saturating_pow(n - 1))
            .unwrap_or(BACKOFF_MAX);
        raw.min(BACKOFF_MAX)
    }

    /// Has the core crashed too many times in the storm window?
    fn storm_tripped(&self) -> bool {
        let now = Instant::now();
        let mut times = self.restart_times.lock().unwrap();
        times.retain(|t| now.duration_since(*t) < STORM_WINDOW);
        times.len() >= STORM_THRESHOLD
    }

    /// Respawn after a crash, honoring backoff + the circuit breaker.
    /// Returns Err if the circuit is open (too many crashes).
    pub fn restart(&self, consecutive: u32) -> std::io::Result<u32> {
        if self.storm_tripped() {
            return Err(std::io::Error::new(
                std::io::ErrorKind::Other,
                "core crash storm — circuit open",
            ));
        }
        std::thread::sleep(self.backoff_for(consecutive));
        self.restart_times.lock().unwrap().push(Instant::now());
        self.spawn()
    }

    /// SIGTERM the core (best effort) on shell quit.
    pub fn stop(&self) {
        if let Some(mut child) = self.child.lock().unwrap().take() {
            let _ = child.kill();
            let _ = child.wait();
        }
    }
}

impl Drop for CoreProcess {
    fn drop(&mut self) {
        self.stop();
    }
}
