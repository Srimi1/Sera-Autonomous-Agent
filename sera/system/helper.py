"""System-level agent helpers — LaunchAgent / systemd / Task Scheduler (P-97).

OUTCLASS: Sera starts on login and responds to a global hotkey without a
terminal open. Rivals require you to run a command first. Sera is just there.

Supported platforms
-------------------
  darwin  — installs a LaunchAgent plist into ~/Library/LaunchAgents/
  linux   — installs a systemd user unit into ~/.config/systemd/user/
  windows — registers a Task Scheduler XML via `schtasks`
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

_HELPER_DIR = Path(__file__).parents[2] / "sera-helper"
_PLIST_NAME   = "com.sera.agent.plist"
_SERVICE_NAME = "sera-agent.service"
_TASK_NAME    = "SeraDaemon"
_TASK_XML     = "sera-agent-task.xml"


@dataclass
class HelperStatus:
    platform: str
    installed: bool
    detail: str


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def install(
    sera_bin: str | None = None,
    sera_home: str | None = None,
    runner: Callable[[list[str]], int] | None = None,
) -> HelperStatus:
    """Install the platform-appropriate daemon helper."""
    bin_path = sera_bin or _find_sera_bin()
    home_path = sera_home or str(Path.home() / ".sera")
    _run = runner or _default_runner
    p = platform.system()
    if p == "Darwin":
        return _install_launchagent(bin_path, home_path, _run)
    if p == "Linux":
        return _install_systemd(bin_path, home_path, _run)
    if p == "Windows":
        return _install_windows_task(bin_path, home_path, _run)
    return HelperStatus(platform=p, installed=False, detail=f"unsupported platform: {p}")


def uninstall(
    runner: Callable[[list[str]], int] | None = None,
) -> HelperStatus:
    """Uninstall the platform-appropriate daemon helper."""
    _run = runner or _default_runner
    p = platform.system()
    if p == "Darwin":
        return _uninstall_launchagent(_run)
    if p == "Linux":
        return _uninstall_systemd(_run)
    if p == "Windows":
        return _uninstall_windows_task(_run)
    return HelperStatus(platform=p, installed=False, detail=f"unsupported platform: {p}")


def status() -> HelperStatus:
    """Return installation status without making any changes."""
    p = platform.system()
    if p == "Darwin":
        dest = _launchagent_dest()
        return HelperStatus(platform=p, installed=dest.exists(), detail=str(dest))
    if p == "Linux":
        dest = _systemd_dest()
        return HelperStatus(platform=p, installed=dest.exists(), detail=str(dest))
    if p == "Windows":
        rc = subprocess.run(
            ["schtasks", "/query", "/tn", _TASK_NAME],
            capture_output=True,
        ).returncode
        return HelperStatus(platform=p, installed=rc == 0, detail=_TASK_NAME)
    return HelperStatus(platform=p, installed=False, detail="unknown")


# ---------------------------------------------------------------------------
# macOS LaunchAgent
# ---------------------------------------------------------------------------

def _launchagent_dest() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / _PLIST_NAME


def _install_launchagent(bin_path: str, home_path: str, runner) -> HelperStatus:
    template = (_HELPER_DIR / _PLIST_NAME).read_text()
    filled   = template.replace("__SERA_BIN__", bin_path).replace("__SERA_HOME__", home_path)
    dest = _launchagent_dest()
    dest.parent.mkdir(parents=True, exist_ok=True)
    Path(home_path, "logs").mkdir(parents=True, exist_ok=True)
    dest.write_text(filled)
    rc = runner(["launchctl", "load", "-w", str(dest)])
    return HelperStatus(
        platform="Darwin",
        installed=rc == 0,
        detail=f"launchctl load rc={rc}  plist={dest}",
    )


def _uninstall_launchagent(runner) -> HelperStatus:
    dest = _launchagent_dest()
    if dest.exists():
        runner(["launchctl", "unload", str(dest)])
        dest.unlink()
    return HelperStatus(platform="Darwin", installed=False, detail=str(dest))


# ---------------------------------------------------------------------------
# Linux systemd user unit
# ---------------------------------------------------------------------------

def _systemd_dest() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
    return Path(xdg) / "systemd" / "user" / _SERVICE_NAME


def _install_systemd(bin_path: str, home_path: str, runner) -> HelperStatus:
    template = (_HELPER_DIR / _SERVICE_NAME).read_text()
    filled   = template.replace("__SERA_BIN__", bin_path).replace("__SERA_HOME__", home_path)
    dest = _systemd_dest()
    dest.parent.mkdir(parents=True, exist_ok=True)
    Path(home_path, "logs").mkdir(parents=True, exist_ok=True)
    dest.write_text(filled)
    runner(["systemctl", "--user", "daemon-reload"])
    rc = runner(["systemctl", "--user", "enable", "--now", _SERVICE_NAME])
    return HelperStatus(
        platform="Linux",
        installed=rc == 0,
        detail=f"systemctl enable rc={rc}  unit={dest}",
    )


def _uninstall_systemd(runner) -> HelperStatus:
    dest = _systemd_dest()
    runner(["systemctl", "--user", "disable", "--now", _SERVICE_NAME])
    if dest.exists():
        dest.unlink()
    runner(["systemctl", "--user", "daemon-reload"])
    return HelperStatus(platform="Linux", installed=False, detail=str(dest))


# ---------------------------------------------------------------------------
# Windows Task Scheduler
# ---------------------------------------------------------------------------

def _install_windows_task(bin_path: str, home_path: str, runner) -> HelperStatus:
    template = (_HELPER_DIR / _TASK_XML).read_text()
    filled   = template.replace("__SERA_BIN__", bin_path).replace("__SERA_HOME__", home_path)
    tmp = Path(home_path) / "sera-task.xml"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(filled, encoding="utf-16")
    rc = runner(["schtasks", "/create", "/tn", _TASK_NAME, "/xml", str(tmp), "/f"])
    tmp.unlink(missing_ok=True)
    return HelperStatus(
        platform="Windows",
        installed=rc == 0,
        detail=f"schtasks /create rc={rc}",
    )


def _uninstall_windows_task(runner) -> HelperStatus:
    rc = runner(["schtasks", "/delete", "/tn", _TASK_NAME, "/f"])
    return HelperStatus(platform="Windows", installed=False, detail=f"schtasks /delete rc={rc}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_sera_bin() -> str:
    found = shutil.which("sera")
    if found:
        return found
    return sys.executable + " -m sera"


def _default_runner(cmd: list[str]) -> int:
    return subprocess.run(cmd, capture_output=True).returncode
