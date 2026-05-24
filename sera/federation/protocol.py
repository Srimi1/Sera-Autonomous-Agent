"""P-92: federation wire protocol — signed requests/responses, consent records.

Every cross-Sera message is Ed25519-signed over its canonical JSON so the
receiver can prove who asked and the asker can prove who answered. The signing
seam reuses the same trust model as `.redpack` / `.skillpack` (P-87/P-28).
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum

try:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
        Ed25519PublicKey,
    )
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PrivateFormat,
        PublicFormat,
        load_pem_private_key,
        load_pem_public_key,
    )
    _CRYPTO = True
except ImportError:  # pragma: no cover
    _CRYPTO = False


class FederationError(RuntimeError):
    """Raised on signature failure, malformed payloads, or protocol violations."""


class ConsentDecision(str, Enum):
    APPROVED = "approved"
    DENIED = "denied"


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------

def generate_peer_keys() -> tuple[bytes, bytes]:
    """Return (private_key_pem, public_key_pem) for a federation peer."""
    if not _CRYPTO:
        raise ImportError("pip install cryptography to use federation")
    priv = Ed25519PrivateKey.generate()
    priv_pem = priv.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    pub_pem = priv.public_key().public_bytes(
        Encoding.PEM, PublicFormat.SubjectPublicKeyInfo
    )
    return priv_pem, pub_pem


def _sign(data: bytes, private_key_pem: bytes) -> bytes:
    if not _CRYPTO:
        raise ImportError("pip install cryptography to sign federation messages")
    priv = load_pem_private_key(private_key_pem, password=None)
    return priv.sign(data)


def _verify(data: bytes, sig: bytes, public_key_pem: bytes) -> None:
    if not _CRYPTO:
        raise ImportError("pip install cryptography to verify federation messages")
    try:
        pub = load_pem_public_key(public_key_pem)
        pub.verify(sig, data)
    except InvalidSignature as e:
        raise FederationError("signature verification failed") from e


# ---------------------------------------------------------------------------
# Request
# ---------------------------------------------------------------------------

@dataclass
class FederationRequest:
    """A question one Sera asks another. Signed by the requester."""

    question: str
    requester_id: str           # human-readable peer name, e.g. "alice"
    requester_pubkey_pem: str   # PEM so the responder can verify + reply
    request_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: float = field(default_factory=time.time)
    signature_hex: str = ""

    def _signable(self) -> bytes:
        body = {
            "question": self.question,
            "requester_id": self.requester_id,
            "requester_pubkey_pem": self.requester_pubkey_pem,
            "request_id": self.request_id,
            "created_at": self.created_at,
        }
        return json.dumps(body, sort_keys=True, separators=(",", ":")).encode()

    def sign(self, private_key_pem: bytes) -> "FederationRequest":
        self.signature_hex = _sign(self._signable(), private_key_pem).hex()
        return self

    def verify(self) -> None:
        if not self.signature_hex:
            raise FederationError("request is unsigned")
        _verify(
            self._signable(),
            bytes.fromhex(self.signature_hex),
            self.requester_pubkey_pem.encode(),
        )

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str) -> "FederationRequest":
        data = json.loads(raw)
        return cls(**data)


# ---------------------------------------------------------------------------
# Response
# ---------------------------------------------------------------------------

@dataclass
class FederationResponse:
    """The answer (or denial) the responding Sera returns. Signed by responder."""

    request_id: str
    decision: str               # ConsentDecision value
    answer: str                 # empty on denial
    responder_id: str
    responder_pubkey_pem: str
    snippets: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    signature_hex: str = ""

    def _signable(self) -> bytes:
        body = {
            "request_id": self.request_id,
            "decision": self.decision,
            "answer": self.answer,
            "responder_id": self.responder_id,
            "responder_pubkey_pem": self.responder_pubkey_pem,
            "snippets": self.snippets,
            "created_at": self.created_at,
        }
        return json.dumps(body, sort_keys=True, separators=(",", ":")).encode()

    def sign(self, private_key_pem: bytes) -> "FederationResponse":
        self.signature_hex = _sign(self._signable(), private_key_pem).hex()
        return self

    def verify(self) -> None:
        if not self.signature_hex:
            raise FederationError("response is unsigned")
        _verify(
            self._signable(),
            bytes.fromhex(self.signature_hex),
            self.responder_pubkey_pem.encode(),
        )

    @property
    def approved(self) -> bool:
        return self.decision == ConsentDecision.APPROVED.value

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str) -> "FederationResponse":
        data = json.loads(raw)
        return cls(**data)
