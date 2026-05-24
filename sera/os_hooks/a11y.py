"""Accessibility tree read — consent-gated (P-70).

Reads the focused app's accessibility tree (macOS AX API / platform a11y) so
Sera can "see" UI it doesn't own. Backend injectable; the ACCESSIBILITY consent
gate is the tested guarantee.
"""
from __future__ import annotations

from typing import Any, Callable

from sera.os_hooks.consent import ConsentManager, Feature

ReadTreeFn = Callable[[], dict[str, Any]]


def _platform_read_tree() -> dict[str, Any]:  # pragma: no cover - needs AX perms
    raise NotImplementedError("platform accessibility backend not wired in this build")


def read_a11y_tree(consent: ConsentManager, *, _read: ReadTreeFn | None = None) -> dict[str, Any]:
    consent.require(Feature.ACCESSIBILITY)
    return (_read or _platform_read_tree)()
