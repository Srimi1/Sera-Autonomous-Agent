"""Encrypted, tamper-evident vault — secrets + approval shape-memory.

OUTCLASS: OpenHuman has an approval flow but stores nothing encrypted. Sera's
vault is AES-256-GCM at rest: every entry is encrypted AND authenticated, so
you cannot hand-edit the file to whitelist `rm -rf` — any tampering fails the
GCM auth tag and the load raises VaultError. The master key lives in the OS
keychain (injectable for tests), never on disk beside the ciphertext.

It holds two things:

  1. Secrets — API keys / tokens, encrypted (an alternative to scattering them
     across keychain entries or, worse, plaintext config).
  2. Approval shape-memory — the P-64 feature. "Approve this exact tool +
     arg-shape once" is remembered so the identical call auto-approves next
     time; a denial arms a 24h cooldown that auto-denies the same shape. The
     fingerprint is a SHA-256 over (tool_name, canonical-JSON(args)), so
     approving `git status` never auto-approves a different command.

Crypto: AES-GCM is authenticated encryption — the 16-byte tag is a MAC over
the ciphertext, so "encrypted" and "signed/tamper-evident" are the same
primitive here. No separate signature needed for a single-process local vault.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from sera.config import SERA_HOME

VAULT_PATH = SERA_HOME / "vault.enc"
KEYCHAIN_SERVICE = "sera"
KEYCHAIN_VAULT_KEY = "vault_master_key"
DEFAULT_DENY_COOLDOWN_S = 24 * 3600     # the P-64 deny cooldown
_NONCE_BYTES = 12
_KEY_BYTES = 32                          # AES-256
_MAGIC = b"SERAV1"                        # format tag, prepended before nonce


class VaultError(Exception):
    """Vault could not be opened — wrong key, corruption, or tampering."""


# ---------------------------------------------------------------------------
# Master key provider — OS keychain by default, injectable for tests
# ---------------------------------------------------------------------------

def keychain_key_provider() -> bytes:
    """Fetch (or mint + store) the vault master key from the OS keychain."""
    import keyring

    existing = keyring.get_password(KEYCHAIN_SERVICE, KEYCHAIN_VAULT_KEY)
    if existing:
        return bytes.fromhex(existing)
    key = AESGCM.generate_key(bit_length=256)
    keyring.set_password(KEYCHAIN_SERVICE, KEYCHAIN_VAULT_KEY, key.hex())
    return key


# ---------------------------------------------------------------------------
# Approval shape-memory
# ---------------------------------------------------------------------------

def fingerprint(tool_name: str, arguments: dict[str, Any]) -> str:
    """Stable SHA-256 over (tool_name, canonical args).

    Canonical JSON (sorted keys, tight separators) means logically-equal arg
    dicts fingerprint identically regardless of key order, while any change to
    a value or the tool name produces a different fingerprint.
    """
    canon = json.dumps(arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    digest = hashlib.sha256(f"{tool_name}\x00{canon}".encode("utf-8")).hexdigest()
    return digest


@dataclass(frozen=True)
class ApprovalRecord:
    fingerprint: str
    decision: bool          # True = allow, False = deny
    tool_name: str
    created_at: float
    expires_at: float | None   # None = never expires (persistent allow)

    def active(self, now: float) -> bool:
        return self.expires_at is None or now < self.expires_at


# ---------------------------------------------------------------------------
# The vault
# ---------------------------------------------------------------------------

class EncryptedVault:
    """AES-256-GCM encrypted, tamper-evident key/value + approval store."""

    def __init__(
        self,
        *,
        path: Path | None = None,
        key: bytes | None = None,
        key_provider: Callable[[], bytes] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._path = path or VAULT_PATH
        self._clock = clock
        if key is not None:
            if len(key) != _KEY_BYTES:
                raise ValueError(f"vault key must be {_KEY_BYTES} bytes")
            self._key = key
        else:
            provider = key_provider or keychain_key_provider
            self._key = provider()
        self._aes = AESGCM(self._key)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    # -- low-level encrypted blob ------------------------------------------

    def _load(self) -> dict[str, Any]:
        if not self._path.exists():
            return {"secrets": {}, "approvals": {}}
        raw = self._path.read_bytes()
        if not raw.startswith(_MAGIC):
            raise VaultError("vault file has an unrecognized format")
        body = raw[len(_MAGIC):]
        nonce, ct = body[:_NONCE_BYTES], body[_NONCE_BYTES:]
        try:
            plain = self._aes.decrypt(nonce, ct, _MAGIC)
        except InvalidTag as exc:
            raise VaultError("vault failed authentication — wrong key or tampering") from exc
        data = json.loads(plain.decode("utf-8"))
        data.setdefault("secrets", {})
        data.setdefault("approvals", {})
        return data

    def _save(self, data: dict[str, Any]) -> None:
        plain = json.dumps(data, separators=(",", ":")).encode("utf-8")
        nonce = os.urandom(_NONCE_BYTES)
        ct = self._aes.encrypt(nonce, plain, _MAGIC)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_bytes(_MAGIC + nonce + ct)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, self._path)   # atomic swap

    # -- secrets ------------------------------------------------------------

    def put_secret(self, name: str, value: str) -> None:
        data = self._load()
        data["secrets"][name] = value
        self._save(data)

    def get_secret(self, name: str) -> str | None:
        return self._load()["secrets"].get(name)

    def delete_secret(self, name: str) -> None:
        data = self._load()
        if name in data["secrets"]:
            del data["secrets"][name]
            self._save(data)

    def list_secret_names(self) -> list[str]:
        return sorted(self._load()["secrets"].keys())

    # -- approval shape-memory ---------------------------------------------

    def remember_approval(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        decision: bool,
        ttl_s: float | None = None,
        deny_cooldown_s: float = DEFAULT_DENY_COOLDOWN_S,
    ) -> ApprovalRecord:
        """Persist a decision for an exact (tool, arg-shape).

        allow → persistent unless ttl_s given.
        deny  → expires after deny_cooldown_s (the 24h cooldown).
        """
        now = self._clock()
        fp = fingerprint(tool_name, arguments)
        if decision:
            expires = (now + ttl_s) if ttl_s is not None else None
        else:
            expires = now + deny_cooldown_s
        record = ApprovalRecord(
            fingerprint=fp, decision=decision, tool_name=tool_name,
            created_at=now, expires_at=expires,
        )
        data = self._load()
        data["approvals"][fp] = {
            "decision": decision,
            "tool_name": tool_name,
            "created_at": now,
            "expires_at": expires,
        }
        self._save(data)
        return record

    def check_approval(self, tool_name: str, arguments: dict[str, Any]) -> ApprovalRecord | None:
        """Return an ACTIVE record for this exact shape, or None.

        Expired records (allow with elapsed TTL, or a deny past its cooldown)
        return None — the caller must prompt again.
        """
        fp = fingerprint(tool_name, arguments)
        row = self._load()["approvals"].get(fp)
        if row is None:
            return None
        rec = ApprovalRecord(
            fingerprint=fp,
            decision=bool(row["decision"]),
            tool_name=row.get("tool_name", tool_name),
            created_at=float(row["created_at"]),
            expires_at=(float(row["expires_at"]) if row["expires_at"] is not None else None),
        )
        if not rec.active(self._clock()):
            return None
        return rec

    def forget_approval(self, tool_name: str, arguments: dict[str, Any]) -> None:
        fp = fingerprint(tool_name, arguments)
        data = self._load()
        if fp in data["approvals"]:
            del data["approvals"][fp]
            self._save(data)

    def clear_approvals(self) -> int:
        data = self._load()
        n = len(data["approvals"])
        data["approvals"] = {}
        self._save(data)
        return n

    # -- key rotation -------------------------------------------------------

    def rotate_key(self, new_key: bytes) -> None:
        """Re-encrypt the vault with a new AES-256 key (atomic).

        1. Decrypt current ciphertext with old key.
        2. Re-encrypt with new_key.
        3. Swap key on self so subsequent calls use new_key.

        The vault file is updated atomically via the existing _save() path.
        Old approvals and secrets remain fully intact after rotation.
        """
        if len(new_key) != _KEY_BYTES:
            raise ValueError(f"new key must be {_KEY_BYTES} bytes, got {len(new_key)}")
        data = self._load()           # decrypt with current (old) key
        self._key = new_key           # switch to new key
        self._aes = AESGCM(new_key)
        self._save(data)              # re-encrypt with new key
