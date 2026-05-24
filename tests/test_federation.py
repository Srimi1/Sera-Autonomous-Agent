"""P-92: federated Sera-to-Sera, consent-only."""
from __future__ import annotations

import pytest

from sera.federation import (
    ConsentDecision,
    FederationError,
    FederationNode,
    FederationRequest,
    FederationResponse,
    generate_peer_keys,
)
from sera.federation.node import ask_peer, deny_all


@pytest.fixture
def alice_keys():
    return generate_peer_keys()


@pytest.fixture
def bob_keys():
    return generate_peer_keys()


def _approve(_req):
    return ConsentDecision.APPROVED


def _fake_search(question):
    return f"Answer to: {question}", ["snippet-1", "snippet-2"]


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------

def test_generate_peer_keys_returns_pair(alice_keys):
    priv, pub = alice_keys
    assert b"PRIVATE KEY" in priv
    assert b"PUBLIC KEY" in pub


def test_keys_are_unique():
    a_priv, _ = generate_peer_keys()
    b_priv, _ = generate_peer_keys()
    assert a_priv != b_priv


# ---------------------------------------------------------------------------
# Request signing / verification
# ---------------------------------------------------------------------------

def test_request_sign_and_verify(alice_keys):
    priv, pub = alice_keys
    req = FederationRequest(
        question="what's my coffee order?",
        requester_id="alice",
        requester_pubkey_pem=pub.decode(),
    ).sign(priv)
    req.verify()  # should not raise


def test_unsigned_request_verify_raises(alice_keys):
    _, pub = alice_keys
    req = FederationRequest(
        question="q", requester_id="alice", requester_pubkey_pem=pub.decode()
    )
    with pytest.raises(FederationError):
        req.verify()


def test_tampered_request_fails_verify(alice_keys):
    priv, pub = alice_keys
    req = FederationRequest(
        question="original", requester_id="alice", requester_pubkey_pem=pub.decode()
    ).sign(priv)
    req.question = "tampered"
    with pytest.raises(FederationError):
        req.verify()


def test_request_json_roundtrip(alice_keys):
    priv, pub = alice_keys
    req = FederationRequest(
        question="q", requester_id="alice", requester_pubkey_pem=pub.decode()
    ).sign(priv)
    req2 = FederationRequest.from_json(req.to_json())
    req2.verify()
    assert req2.question == "q"
    assert req2.request_id == req.request_id


# ---------------------------------------------------------------------------
# Response signing / verification
# ---------------------------------------------------------------------------

def test_response_sign_and_verify(bob_keys):
    priv, pub = bob_keys
    resp = FederationResponse(
        request_id="r1",
        decision=ConsentDecision.APPROVED.value,
        answer="hi",
        responder_id="bob",
        responder_pubkey_pem=pub.decode(),
    ).sign(priv)
    resp.verify()


def test_response_approved_property(bob_keys):
    _, pub = bob_keys
    resp = FederationResponse(
        request_id="r1",
        decision=ConsentDecision.APPROVED.value,
        answer="x",
        responder_id="bob",
        responder_pubkey_pem=pub.decode(),
    )
    assert resp.approved
    resp.decision = ConsentDecision.DENIED.value
    assert not resp.approved


# ---------------------------------------------------------------------------
# FederationNode — consent gate
# ---------------------------------------------------------------------------

def test_node_denies_by_default(alice_keys, bob_keys):
    a_priv, a_pub = alice_keys
    b_priv, b_pub = bob_keys
    node = FederationNode(
        node_id="bob",
        private_key_pem=b_priv,
        public_key_pem=b_pub,
        memory_search=_fake_search,
    )
    resp = ask_peer(
        question="secret?",
        requester_id="alice",
        requester_priv_pem=a_priv,
        requester_pub_pem=a_pub,
        peer=node,
    )
    assert not resp.approved
    assert resp.answer == ""


def test_node_approves_and_answers(alice_keys, bob_keys):
    a_priv, a_pub = alice_keys
    b_priv, b_pub = bob_keys
    node = FederationNode(
        node_id="bob",
        private_key_pem=b_priv,
        public_key_pem=b_pub,
        consent_gate=_approve,
        memory_search=_fake_search,
    )
    resp = ask_peer(
        question="what's my coffee order?",
        requester_id="alice",
        requester_priv_pem=a_priv,
        requester_pub_pem=a_pub,
        peer=node,
    )
    assert resp.approved
    assert "what's my coffee order?" in resp.answer
    assert resp.snippets == ["snippet-1", "snippet-2"]


def test_node_rejects_bad_signature(alice_keys, bob_keys):
    a_priv, a_pub = alice_keys
    b_priv, b_pub = bob_keys
    node = FederationNode(
        node_id="bob", private_key_pem=b_priv, public_key_pem=b_pub,
        consent_gate=_approve, memory_search=_fake_search,
    )
    req = FederationRequest(
        question="q", requester_id="alice", requester_pubkey_pem=a_pub.decode()
    ).sign(a_priv)
    req.question = "tampered after signing"
    with pytest.raises(FederationError):
        node.handle(req)


def test_node_approved_without_search_raises(alice_keys, bob_keys):
    a_priv, a_pub = alice_keys
    b_priv, b_pub = bob_keys
    node = FederationNode(
        node_id="bob", private_key_pem=b_priv, public_key_pem=b_pub,
        consent_gate=_approve, memory_search=None,
    )
    req = FederationRequest(
        question="q", requester_id="alice", requester_pubkey_pem=a_pub.decode()
    ).sign(a_priv)
    with pytest.raises(FederationError):
        node.handle(req)


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

def test_audit_log_records_each_query(alice_keys, bob_keys):
    a_priv, a_pub = alice_keys
    b_priv, b_pub = bob_keys
    node = FederationNode(
        node_id="bob", private_key_pem=b_priv, public_key_pem=b_pub,
        consent_gate=_approve, memory_search=_fake_search,
    )
    for q in ["q1", "q2"]:
        ask_peer(
            question=q, requester_id="alice",
            requester_priv_pem=a_priv, requester_pub_pem=a_pub, peer=node,
        )
    assert len(node.audit_log) == 2
    assert node.audit_log[0].question == "q1"
    assert node.audit_log[0].decision == "approved"


def test_audit_log_records_denials(alice_keys, bob_keys):
    a_priv, a_pub = alice_keys
    b_priv, b_pub = bob_keys
    node = FederationNode(
        node_id="bob", private_key_pem=b_priv, public_key_pem=b_pub,
        consent_gate=deny_all, memory_search=_fake_search,
    )
    ask_peer(
        question="private", requester_id="alice",
        requester_priv_pem=a_priv, requester_pub_pem=a_pub, peer=node,
    )
    assert node.audit_log[0].decision == "denied"


# ---------------------------------------------------------------------------
# JSON wire path
# ---------------------------------------------------------------------------

def test_handle_json_roundtrip(alice_keys, bob_keys):
    a_priv, a_pub = alice_keys
    b_priv, b_pub = bob_keys
    node = FederationNode(
        node_id="bob", private_key_pem=b_priv, public_key_pem=b_pub,
        consent_gate=_approve, memory_search=_fake_search,
    )
    req = FederationRequest(
        question="ping", requester_id="alice", requester_pubkey_pem=a_pub.decode()
    ).sign(a_priv)
    raw_resp = node.handle_json(req.to_json())
    resp = FederationResponse.from_json(raw_resp)
    resp.verify()
    assert resp.approved


def test_full_a_to_b_flow_with_per_question_consent(alice_keys, bob_keys):
    """A asks B; B's user approves only the coffee question, denies the rest."""
    a_priv, a_pub = alice_keys
    b_priv, b_pub = bob_keys

    def selective_consent(req: FederationRequest) -> ConsentDecision:
        if "coffee" in req.question:
            return ConsentDecision.APPROVED
        return ConsentDecision.DENIED

    node = FederationNode(
        node_id="bob", private_key_pem=b_priv, public_key_pem=b_pub,
        consent_gate=selective_consent, memory_search=_fake_search,
    )

    approved = ask_peer(
        question="what's bob's coffee order?", requester_id="alice",
        requester_priv_pem=a_priv, requester_pub_pem=a_pub, peer=node,
    )
    denied = ask_peer(
        question="what's bob's bank password?", requester_id="alice",
        requester_priv_pem=a_priv, requester_pub_pem=a_pub, peer=node,
    )
    assert approved.approved
    assert not denied.approved
