"""P-63: OS-agnostic native notifications fired by Sera's own loop events.

OUTCLASS: Rivals notify on user messages. Sera notifies on its *own* internal
events — memory consolidation complete, LoRA gain recorded, injection detected.
Zero call-site changes needed; wire any loop event to `fire()`.

Usage:
    from sera.notifications import fire, NotificationEvent
    fire(NotificationEvent(title="Sera", body="Memory consolidated (42 chunks)"))

Build Tauri tray: stubs in `_tray_state_path` are consumed by sera-shell.
"""
from __future__ import annotations

import json
import platform
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


Runner = Callable[[list[str]], tuple[int, str, str]]


@dataclass
class NotificationEvent:
    title: str
    body: str
    subtitle: str = ""
    tag: str = ""


@dataclass
class NotificationResult:
    sent: bool
    backend: str
    error: str = ""


def _default_runner(cmd: list[str]) -> tuple[int, str, str]:
    import subprocess
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
    return r.returncode, r.stdout, r.stderr


def _tray_state_path() -> Path:
    """Path consumed by sera-shell Tauri tray to render status."""
    return Path.home() / ".sera" / "tray_state.json"


def _write_tray_state(event: NotificationEvent) -> None:
    path = _tray_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    state = {
        "title": event.title,
        "body": event.body,
        "tag": event.tag,
    }
    path.write_text(json.dumps(state))


def _fire_macos(event: NotificationEvent, runner: Runner) -> NotificationResult:
    subtitle = event.subtitle or ""
    script = (
        f'display notification "{event.body}" '
        f'with title "{event.title}"'
        + (f' subtitle "{subtitle}"' if subtitle else "")
    )
    code, _, err = runner(["osascript", "-e", script])
    if code != 0:
        return NotificationResult(sent=False, backend="osascript", error=err.strip())
    return NotificationResult(sent=True, backend="osascript")


def _fire_linux(event: NotificationEvent, runner: Runner) -> NotificationResult:
    cmd = ["notify-send", event.title, event.body]
    if event.subtitle:
        cmd += ["--hint", f"string:subtitle:{event.subtitle}"]
    code, _, err = runner(cmd)
    if code != 0:
        return NotificationResult(sent=False, backend="notify-send", error=err.strip())
    return NotificationResult(sent=True, backend="notify-send")


def _fire_windows(event: NotificationEvent, runner: Runner) -> NotificationResult:
    ps = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        f'$n = New-Object System.Windows.Forms.NotifyIcon; '
        f'$n.Icon = [System.Drawing.SystemIcons]::Information; '
        f'$n.Visible = $true; '
        f'$n.ShowBalloonTip(3000, "{event.title}", "{event.body}", '
        f"[System.Windows.Forms.ToolTipIcon]::Info)"
    )
    code, _, err = runner(["powershell", "-Command", ps])
    if code != 0:
        return NotificationResult(sent=False, backend="powershell", error=err.strip())
    return NotificationResult(sent=True, backend="powershell")


def fire(
    event: NotificationEvent,
    *,
    runner: Runner | None = None,
    write_tray: bool = True,
) -> NotificationResult:
    """Fire a native OS notification for a Sera loop event."""
    r = runner or _default_runner
    if write_tray:
        try:
            _write_tray_state(event)
        except OSError:
            pass

    system = platform.system()
    if system == "Darwin":
        return _fire_macos(event, r)
    if system == "Linux":
        return _fire_linux(event, r)
    if system == "Windows":
        return _fire_windows(event, r)
    return NotificationResult(sent=False, backend="none", error=f"unsupported OS: {system}")


# ---------------------------------------------------------------------------
# Loop-event helpers — wire directly from dream / audit / train callbacks
# ---------------------------------------------------------------------------

def notify_consolidation(chunk_count: int, *, runner: Runner | None = None) -> NotificationResult:
    return fire(
        NotificationEvent(
            title="Sera Memory",
            body=f"Consolidated {chunk_count} chunks.",
            tag="consolidation",
        ),
        runner=runner,
    )


def notify_lora_gain(gain: float, *, runner: Runner | None = None) -> NotificationResult:
    return fire(
        NotificationEvent(
            title="Sera Training",
            body=f"LoRA gain recorded: {gain:+.4f}",
            tag="lora",
        ),
        runner=runner,
    )


def notify_injection_detected(detail: str, *, runner: Runner | None = None) -> NotificationResult:
    return fire(
        NotificationEvent(
            title="Sera Security",
            body=f"Injection attempt blocked: {detail}",
            tag="injection",
        ),
        runner=runner,
    )
