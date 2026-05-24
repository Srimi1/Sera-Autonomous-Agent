"""P-92: FederationNode — answers peer queries behind a per-question consent gate.

The node verifies the incoming request signature, asks the local consent gate
(a callback the user wires to a UI prompt), and only on APPROVED runs the
injected memory search and returns a signed answer. Every decision is appended
to an in-memory audit log so the user can see who asked what.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

from sera.federation.protocol import (
    ConsentDecision,
    FederationError,
    FederationRequest,
    FederationResponse,
)

# A consent gate: given a verified request, return APPROVED or DENIED.
ConsentGate = Callable[[FederationRequest], ConsentDecision]

# A memory search: given the question, return (answer_text, supporting_snippets).
MemorySearch = Callable[[str], tuple[str, list[str]]]


@dataclass
class AuditEntry:
    request_id: str
    requester_id: str
    question: str
    decision: str
    at: float = field(default_factory=time.time)


def deny_all(_req: FederationRequest) -> ConsentDecision:
    """Safe default gate — denies everything until the user wires a real prompt."""
    return ConsentDecision.DENIED


class FederationNode:
    """Responds to federated queries from peer Seras, consent-gated."""

    def __init__(
        self,
        *,
        node_id: str,
        private_key_pem: bytes,
        public_key_pem: bytes,
        consent_gate: ConsentGate | None = None,
        memory_search: MemorySearch | None = None,
    ) -> None:
        self.node_id = node_id
        self._priv = private_key_pem
        self._pub = public_key_pem
        self._consent = consent_gate or deny_all
        self._search = memory_search
        self.audit_log: list[AuditEntry] = []

    def handle(self, request: FederationRequest) -> FederationResponse:
        """Verify, consent-gate, and answer (or deny) an incoming request."""
        request.verify()  # raises FederationError on bad signature

        decision = self._consent(request)
        self.audit_log.append(
            AuditEntry(
                request_id=request.request_id,
                requester_id=request.requester_id,
                question=request.question,
                decision=decision.value,
            )
        )

        if decision != ConsentDecision.APPROVED:
            return self._sign_response(
                FederationResponse(
                    request_id=request.request_id,
                    decision=ConsentDecision.DENIED.value,
                    answer="",
                    responder_id=self.node_id,
                    responder_pubkey_pem=self._pub.decode(),
                )
            )

        if self._search is None:
            raise FederationError("approved but no memory_search configured")

        answer, snippets = self._search(request.question)
        return self._sign_response(
            FederationResponse(
                request_id=request.request_id,
                decision=ConsentDecision.APPROVED.value,
                answer=answer,
                responder_id=self.node_id,
                responder_pubkey_pem=self._pub.decode(),
                snippets=list(snippets),
            )
        )

    def handle_json(self, raw: str) -> str:
        """Wire entry point: JSON request in, JSON response out."""
        request = FederationRequest.from_json(raw)
        return self.handle(request).to_json()

    def _sign_response(self, resp: FederationResponse) -> FederationResponse:
        return resp.sign(self._priv)


def ask_peer(
    *,
    question: str,
    requester_id: str,
    requester_priv_pem: bytes,
    requester_pub_pem: bytes,
    peer: FederationNode,
) -> FederationResponse:
    """Convenience: build + sign a request, send to a local peer node, verify the reply."""
    req = FederationRequest(
        question=question,
        requester_id=requester_id,
        requester_pubkey_pem=requester_pub_pem.decode(),
    ).sign(requester_priv_pem)
    resp = peer.handle(req)
    resp.verify()
    return resp
