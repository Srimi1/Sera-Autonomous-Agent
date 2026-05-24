"""Clipboard read/write — consent-gated (P-70).

The OS clipboard backend is injectable; the consent gate is the guaranteed,
tested behavior. No clipboard access without active CLIPBOARD consent.
"""
from __future__ import annotations

import subprocess
from typing import Callable

from sera.os_hooks.consent import ConsentManager, Feature

ReadFn = Callable[[], str]
WriteFn = Callable[[str], None]


def _macos_read() -> str:  # pragma: no cover - needs a desktop
    return subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=10).stdout


def _macos_write(text: str) -> None:  # pragma: no cover - needs a desktop
    subprocess.run(["pbcopy"], input=text.encode("utf-8"), timeout=10)


def read_clipboard(consent: ConsentManager, *, _read: ReadFn | None = None) -> str:
    consent.require(Feature.CLIPBOARD)
    return (_read or _macos_read)()


def write_clipboard(consent: ConsentManager, text: str, *, _write: WriteFn | None = None) -> None:
    consent.require(Feature.CLIPBOARD)
    (_write or _macos_write)(text)
