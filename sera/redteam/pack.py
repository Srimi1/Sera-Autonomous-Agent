"""Signed `.redpack` — distributable adversarial payload bundles (P-87).

OUTCLASS: Nobody ships community attack packs as verified artifacts.
A `.redpack` is a zip carrying a payload catalogue (JSON), a SHA-256
manifest, and an optional Ed25519 signature — the same trust model as
`.skillpack` (P-28), applied to red-team payloads.

Format
------
  payloads.json   — list of {id, kind, text, author?, tags?}
  manifest.json   — {"payloads.json": "<sha256-hex>"}
  SIGNATURE.b64   — (optional) base64(Ed25519 sig over manifest.json bytes)

Usage
-----
    from sera.redteam.pack import RedPackBuilder, load_redpack, verify_redpack

    # Build + sign
    b = RedPackBuilder()
    b.add(id="ignore_01", kind="IGNORE", text="Ignore all previous instructions.")
    b.save("custom.redpack", private_key_pem=priv_pem)

    # Load + verify
    payloads = load_redpack("custom.redpack", public_key_pem=pub_pem)
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

try:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PrivateFormat,
        PublicFormat,
        load_pem_private_key,
        load_pem_public_key,
    )
    _CRYPTO = True
except ImportError:
    _CRYPTO = False

_PAYLOAD_FILE = "payloads.json"
_MANIFEST_FILE = "manifest.json"
_SIG_FILE = "SIGNATURE.b64"

VALID_KINDS = {"IGNORE", "ROLE_SWITCH", "EXFIL", "OVERRIDE", "NESTED", "CUSTOM"}


# ---------------------------------------------------------------------------
# Data shape
# ---------------------------------------------------------------------------

@dataclass
class RedPayload:
    id: str
    kind: str
    text: str
    author: str = ""
    tags: list[str] = field(default_factory=list)

    def validate(self) -> None:
        if not self.id or not self.id.replace("-", "").replace("_", "").isalnum():
            raise ValueError(f"invalid payload id: {self.id!r}")
        if self.kind not in VALID_KINDS:
            raise ValueError(f"unknown kind {self.kind!r}; valid: {sorted(VALID_KINDS)}")
        if not self.text.strip():
            raise ValueError("payload text must not be empty")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class RedPackError(RuntimeError):
    """Raised on malformed, tampered, or unverifiable redpacks."""


# ---------------------------------------------------------------------------
# Key helpers (thin wrapper over cryptography)
# ---------------------------------------------------------------------------

def generate_keypair() -> tuple[bytes, bytes]:
    """Return (private_key_pem, public_key_pem)."""
    if not _CRYPTO:
        raise ImportError("pip install cryptography to use signed redpacks")
    priv = Ed25519PrivateKey.generate()
    priv_pem = priv.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    pub_pem = priv.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
    return priv_pem, pub_pem


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------

class RedPackBuilder:
    """Accumulate payloads and write a signed or unsigned .redpack."""

    def __init__(self) -> None:
        self._payloads: list[RedPayload] = []

    def add(
        self,
        *,
        id: str,
        kind: str,
        text: str,
        author: str = "",
        tags: list[str] | None = None,
    ) -> "RedPackBuilder":
        p = RedPayload(id=id, kind=kind, text=text, author=author, tags=tags or [])
        p.validate()
        self._payloads.append(p)
        return self

    def add_payload(self, payload: RedPayload) -> "RedPackBuilder":
        payload.validate()
        self._payloads.append(payload)
        return self

    def payloads(self) -> list[RedPayload]:
        return list(self._payloads)

    def save(
        self,
        out_path: str | Path,
        *,
        private_key_pem: bytes | None = None,
    ) -> None:
        """Write the .redpack to `out_path`."""
        if not self._payloads:
            raise RedPackError("cannot save empty redpack")
        payload_bytes = json.dumps(
            [asdict(p) for p in self._payloads],
            indent=2, ensure_ascii=False,
        ).encode("utf-8")
        sha256 = hashlib.sha256(payload_bytes).hexdigest()
        manifest = {_PAYLOAD_FILE: sha256}
        manifest_bytes = json.dumps(manifest, sort_keys=True,
                                    separators=(",", ":")).encode("utf-8")

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(_PAYLOAD_FILE, payload_bytes)
            zf.writestr(_MANIFEST_FILE, manifest_bytes)
            if private_key_pem is not None:
                sig = _sign(manifest_bytes, private_key_pem)
                zf.writestr(_SIG_FILE, base64.b64encode(sig).decode("ascii"))

        Path(out_path).write_bytes(buf.getvalue())


# ---------------------------------------------------------------------------
# Load + verify
# ---------------------------------------------------------------------------

def load_redpack(
    path: str | Path,
    *,
    public_key_pem: bytes | None = None,
) -> list[RedPayload]:
    """Load and optionally verify a .redpack. Returns list of RedPayload."""
    raw = Path(path).read_bytes()
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            names = zf.namelist()
            if _MANIFEST_FILE not in names:
                raise RedPackError("missing manifest.json")
            if _PAYLOAD_FILE not in names:
                raise RedPackError("missing payloads.json")

            manifest_bytes = zf.read(_MANIFEST_FILE)
            payload_bytes = zf.read(_PAYLOAD_FILE)

            # Verify manifest hashes
            manifest = json.loads(manifest_bytes)
            actual_sha = hashlib.sha256(payload_bytes).hexdigest()
            expected_sha = manifest.get(_PAYLOAD_FILE)
            if actual_sha != expected_sha:
                raise RedPackError("hash mismatch: payloads.json has been tampered")

            # Verify signature if key supplied
            if public_key_pem is not None:
                if _SIG_FILE not in names:
                    raise RedPackError("redpack is unsigned; cannot verify")
                sig_b64 = zf.read(_SIG_FILE).decode("ascii")
                sig = base64.b64decode(sig_b64)
                _verify(manifest_bytes, sig, public_key_pem)

            payloads_raw: list[dict[str, Any]] = json.loads(payload_bytes)
    except zipfile.BadZipFile as e:
        raise RedPackError(f"not a valid redpack: {e}") from e

    result: list[RedPayload] = []
    for raw_p in payloads_raw:
        p = RedPayload(
            id=raw_p["id"],
            kind=raw_p["kind"],
            text=raw_p["text"],
            author=raw_p.get("author", ""),
            tags=raw_p.get("tags") or [],
        )
        result.append(p)
    return result


def verify_redpack(path: str | Path, public_key_pem: bytes) -> None:
    """Raise RedPackError if the redpack signature or hashes are invalid."""
    load_redpack(path, public_key_pem=public_key_pem)


# ---------------------------------------------------------------------------
# Run payloads from a redpack
# ---------------------------------------------------------------------------

def run_redpack(
    path: str | Path,
    *,
    public_key_pem: bytes | None = None,
) -> list[dict[str, Any]]:
    """Load payloads and return them as plain dicts (for RedAgent injection)."""
    payloads = load_redpack(path, public_key_pem=public_key_pem)
    return [asdict(p) for p in payloads]


# ---------------------------------------------------------------------------
# Crypto helpers
# ---------------------------------------------------------------------------

def _sign(data: bytes, private_key_pem: bytes) -> bytes:
    if not _CRYPTO:
        raise ImportError("pip install cryptography to sign redpacks")
    priv = load_pem_private_key(private_key_pem, password=None)
    return priv.sign(data)


def _verify(data: bytes, sig: bytes, public_key_pem: bytes) -> None:
    if not _CRYPTO:
        raise ImportError("pip install cryptography to verify redpacks")
    try:
        pub = load_pem_public_key(public_key_pem)
        pub.verify(sig, data)
    except InvalidSignature as e:
        raise RedPackError("signature verification failed") from e
