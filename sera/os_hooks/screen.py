"""Screen capture — consent-gated (P-70).

The capture itself is platform-specific (macOS `screencapture`, or `mss` on
other platforms) and cannot run headless. What IS guaranteed and tested: no
capture happens without active SCREEN consent — the gate runs before any OS
call. Revoke → the next capture raises ConsentDenied.
"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import Callable

from sera.os_hooks.consent import ConsentManager, Feature

# Injectable capture backend for tests: (out_path) -> None.
CaptureFn = Callable[[Path], None]


def _macos_capture(out: Path) -> None:  # pragma: no cover - needs a display
    subprocess.run(["screencapture", "-x", str(out)], check=True, timeout=30)


def capture_screen(
    consent: ConsentManager,
    *,
    out_path: Path | None = None,
    _capture: CaptureFn | None = None,
) -> Path:
    """Capture the screen to a PNG — only with active SCREEN consent."""
    consent.require(Feature.SCREEN)            # the gate — always first
    out = out_path or Path(tempfile.mktemp(suffix=".png"))
    (_capture or _macos_capture)(out)
    return out
