"""Synthetic keyboard input — consent-gated (P-70).

Lets Sera type into other apps. This is the most dangerous OS hook, so the
KEYBOARD consent gate is mandatory and tested. Backend injectable.
"""
from __future__ import annotations

from typing import Callable

from sera.os_hooks.consent import ConsentManager, Feature

TypeFn = Callable[[str], None]


def _platform_type(text: str) -> None:  # pragma: no cover - needs input perms
    raise NotImplementedError("platform keyboard backend not wired in this build")


def type_text(consent: ConsentManager, text: str, *, _type: TypeFn | None = None) -> None:
    consent.require(Feature.KEYBOARD)
    (_type or _platform_type)(text)
