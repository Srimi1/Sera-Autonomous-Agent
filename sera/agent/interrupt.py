"""Per-turn cancellation token + SIGINT installer.

The CLI binds Ctrl+C to `token.set()` for the lifetime of a single turn.
The agent loop checks the token at iteration boundaries and after every
tool result. The streaming text path is not interruptible mid-chunk by
design — the provider stream finishes its in-flight delta first so the
session row stays consistent, then control returns.

Returning control within ~200ms (the verification target) requires that
the budget be checked after each tool — long-running tools are the
common stall, and tools cannot be safely killed without leaving a
half-written file or a child shell behind.
"""
from __future__ import annotations

import signal
import threading
from contextlib import contextmanager
from typing import Iterator


class Interrupted(RuntimeError):
    """Raised by the loop when the active turn's cancel token is set."""


class InterruptToken:
    """Threadsafe one-shot cancel flag.

    Created fresh per `run_turn`. Reuse across turns is not supported —
    a stale True flag would abort the next turn before it starts.
    """

    def __init__(self) -> None:
        self._event = threading.Event()

    def set(self) -> None:
        self._event.set()

    def is_set(self) -> bool:
        return self._event.is_set()

    def check(self) -> None:
        """Raise Interrupted if the token has fired."""
        if self._event.is_set():
            raise Interrupted("turn cancelled by user")


@contextmanager
def install_sigint(token: InterruptToken) -> Iterator[None]:
    """Route the next SIGINT to `token.set()` for the body of the `with`.

    Restores whatever handler was in place on exit, even if the body
    raises. A second SIGINT during the same turn re-raises
    KeyboardInterrupt so the REPL can still exit on a double Ctrl+C.
    """
    fired = {"once": False}

    def _handler(signum, frame):  # noqa: ANN001 — signal handler signature
        if fired["once"]:
            # Second Ctrl+C: hand control back to whoever installed us next.
            raise KeyboardInterrupt
        fired["once"] = True
        token.set()

    previous = signal.signal(signal.SIGINT, _handler)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)
