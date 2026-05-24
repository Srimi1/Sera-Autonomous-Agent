"""P-92: Federated Sera-to-Sera, consent-only.

OUTCLASS: Per-question consent. A friend's Sera asks your Sera a question; your
Sera answers from your memory ONLY if you approve that exact question once. No
standing access, no blanket sharing — each query is gated, signed, and logged.
"""
from sera.federation.protocol import (
    ConsentDecision,
    FederationError,
    FederationRequest,
    FederationResponse,
    generate_peer_keys,
)
from sera.federation.node import FederationNode

__all__ = [
    "ConsentDecision",
    "FederationError",
    "FederationRequest",
    "FederationResponse",
    "FederationNode",
    "generate_peer_keys",
]
