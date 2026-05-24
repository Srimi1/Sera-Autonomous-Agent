"""Signed, per-feature consent for OS-level capabilities (P-70).

OUTCLASS: per-feature consent toggles stored in the encrypted vault (P-64), so
each grant is tamper-evident — you cannot hand-edit a file to turn screen
capture back on after revoking it; the vault's AES-GCM tag would fail. Revoke
flips a capability off in one call, immediately, with no process restart. No
rival ships granular, signed, instantly-revocable OS consent.

Every OS hook (screen, clipboard, a11y, keyboard) calls `require()` before it
touches the system. The consent map lives as one authenticated blob inside the
EncryptedVault under a reserved key.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable

from sera.safety.vault import EncryptedVault

_CONSENT_KEY = "__os_consent__"


class Feature(str, Enum):
    SCREEN = "screen"
    CLIPBOARD = "clipboard"
    ACCESSIBILITY = "accessibility"
    KEYBOARD = "keyboard"


class ConsentDenied(PermissionError):
    """An OS hook was invoked without active consent for its feature."""

    def __init__(self, feature: "Feature | str") -> None:
        f = feature.value if isinstance(feature, Feature) else str(feature)
        super().__init__(
            f"consent not granted for '{f}'. Enable it in Settings → Consent."
        )
        self.feature = f


@dataclass(frozen=True)
class ConsentRecord:
    feature: str
    granted: bool
    granted_at: float
    expires_at: float | None     # None = until revoked


class ConsentManager:
    """Grant / revoke / check per-feature OS consent, persisted encrypted."""

    def __init__(self, vault: EncryptedVault, *, clock: Callable[[], float] = time.time) -> None:
        self._vault = vault
        self._clock = clock

    def _load(self) -> dict[str, dict[str, Any]]:
        raw = self._vault.get_secret(_CONSENT_KEY)
        if not raw:
            return {}
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}

    def _save(self, data: dict[str, dict[str, Any]]) -> None:
        self._vault.put_secret(_CONSENT_KEY, json.dumps(data, separators=(",", ":")))

    @staticmethod
    def _key(feature: Feature | str) -> str:
        return feature.value if isinstance(feature, Feature) else str(feature)

    def grant(self, feature: Feature | str, *, ttl_s: float | None = None) -> ConsentRecord:
        now = self._clock()
        key = self._key(feature)
        expires = (now + ttl_s) if ttl_s is not None else None
        data = self._load()
        data[key] = {"granted": True, "granted_at": now, "expires_at": expires}
        self._save(data)
        return ConsentRecord(feature=key, granted=True, granted_at=now, expires_at=expires)

    def revoke(self, feature: Feature | str) -> None:
        """One-click off. The capability is refused on the very next call."""
        key = self._key(feature)
        data = self._load()
        if key in data:
            del data[key]
            self._save(data)

    def is_allowed(self, feature: Feature | str) -> bool:
        key = self._key(feature)
        row = self._load().get(key)
        if not row or not row.get("granted"):
            return False
        exp = row.get("expires_at")
        if exp is not None and self._clock() >= float(exp):
            return False
        return True

    def require(self, feature: Feature | str) -> None:
        """Raise ConsentDenied unless `feature` is actively consented."""
        if not self.is_allowed(feature):
            raise ConsentDenied(feature)

    def status(self) -> dict[str, bool]:
        """Map every known feature → currently-allowed, for the Settings panel."""
        return {f.value: self.is_allowed(f) for f in Feature}
