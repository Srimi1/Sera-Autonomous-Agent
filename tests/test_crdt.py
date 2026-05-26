"""P-91: CRDT memory sync — LWWRegister, ORSet, CRDTDocument, RelayStub."""
from __future__ import annotations



from sera.sync.crdt import (
    CRDTDocument,
    LWWRegister,
    ORSet,
    RelayStub,
    merge,
)


# ---------------------------------------------------------------------------
# LWWRegister
# ---------------------------------------------------------------------------

def test_lww_set_get():
    reg = LWWRegister()
    reg.set("name", "Alice", node_id="n1")
    assert reg.get("name") == "Alice"


def test_lww_default():
    reg = LWWRegister()
    assert reg.get("missing") is None
    assert reg.get("missing", "default") == "default"


def test_lww_later_ts_wins():
    reg = LWWRegister()
    reg.set("x", "old", node_id="n1", ts=1.0)
    reg.set("x", "new", node_id="n2", ts=2.0)
    assert reg.get("x") == "new"


def test_lww_earlier_ts_loses():
    reg = LWWRegister()
    reg.set("x", "current", node_id="n1", ts=2.0)
    reg.set("x", "stale", node_id="n2", ts=1.0)
    assert reg.get("x") == "current"


def test_lww_tiebreak_by_node_id():
    reg = LWWRegister()
    reg.set("x", "beta", node_id="beta", ts=1.0)
    reg.set("x", "alpha", node_id="alpha", ts=1.0)
    # "beta" > "alpha" lexicographically
    assert reg.get("x") == "beta"


def test_lww_merge_remote_wins_later_ts():
    local = LWWRegister()
    local.set("k", "v1", node_id="n1", ts=1.0)
    remote = LWWRegister()
    remote.set("k", "v2", node_id="n2", ts=2.0)
    local.merge(remote)
    assert local.get("k") == "v2"


def test_lww_merge_local_wins_later_ts():
    local = LWWRegister()
    local.set("k", "v1", node_id="n1", ts=3.0)
    remote = LWWRegister()
    remote.set("k", "v2", node_id="n2", ts=1.0)
    local.merge(remote)
    assert local.get("k") == "v1"


def test_lww_merge_new_key_from_remote():
    local = LWWRegister()
    remote = LWWRegister()
    remote.set("new_key", "value", node_id="n1")
    local.merge(remote)
    assert local.get("new_key") == "value"


def test_lww_roundtrip_dict():
    reg = LWWRegister()
    reg.set("a", 42, node_id="node1", ts=100.0)
    reg2 = LWWRegister.from_dict(reg.to_dict())
    assert reg2.get("a") == 42


# ---------------------------------------------------------------------------
# ORSet
# ---------------------------------------------------------------------------

def test_orset_add_contains():
    s = ORSet()
    s.add("chunk-1", node_id="n1")
    assert s.contains("chunk-1")


def test_orset_remove():
    s = ORSet()
    s.add("chunk-1", node_id="n1")
    s.remove("chunk-1")
    assert not s.contains("chunk-1")


def test_orset_add_after_remove_wins():
    s = ORSet()
    s.add("chunk-1", node_id="n1")
    s.remove("chunk-1")
    s.add("chunk-1", node_id="n1")
    assert s.contains("chunk-1")


def test_orset_concurrent_add_wins_over_remove():
    """Concurrent add from another node survives remote remove."""
    local = ORSet()
    local.add("chunk-1", node_id="n1")

    remote = ORSet()
    remote.add("chunk-1", node_id="n2")
    remote.remove("chunk-1")  # removes n2's uid only in remote

    local.merge(remote)
    # n1's add uid is still live
    assert local.contains("chunk-1")


def test_orset_elements():
    s = ORSet()
    s.add("c", node_id="n1")
    s.add("a", node_id="n1")
    s.add("b", node_id="n1")
    assert s.elements() == ["a", "b", "c"]


def test_orset_elements_after_remove():
    s = ORSet()
    s.add("keep", node_id="n1")
    s.add("drop", node_id="n1")
    s.remove("drop")
    assert s.elements() == ["keep"]


def test_orset_merge_idempotent():
    s = ORSet()
    s.add("x", node_id="n1")
    s.merge(s)  # merge with itself
    assert s.elements() == ["x"]


def test_orset_roundtrip_dict():
    s = ORSet()
    s.add("x", node_id="n1", ts=1.0)
    s2 = ORSet.from_dict(s.to_dict())
    assert s2.contains("x")


# ---------------------------------------------------------------------------
# CRDTDocument
# ---------------------------------------------------------------------------

def test_crdt_doc_chunks():
    doc = CRDTDocument()
    doc.chunks.add("c1", node_id="n1")
    assert doc.chunks.contains("c1")


def test_crdt_doc_entities():
    doc = CRDTDocument()
    doc.entities.set("user_name", "Alice", node_id="n1")
    assert doc.entities.get("user_name") == "Alice"


def test_crdt_doc_relations():
    doc = CRDTDocument()
    doc.relations.set("alice->bob", "knows", node_id="n1")
    assert doc.relations.get("alice->bob") == "knows"


def test_crdt_doc_merge_function():
    local = CRDTDocument()
    local.chunks.add("c1", node_id="n1")
    local.entities.set("x", "1", node_id="n1", ts=1.0)

    remote = CRDTDocument()
    remote.chunks.add("c2", node_id="n2")
    remote.entities.set("x", "2", node_id="n2", ts=2.0)

    result = merge(local, remote)
    assert result.chunks.contains("c1")
    assert result.chunks.contains("c2")
    assert result.entities.get("x") == "2"


def test_crdt_doc_merge_commutative():
    """merge(A, B) == merge(B, A) in terms of observable state."""
    a = CRDTDocument()
    a.entities.set("k", "from_a", node_id="a", ts=1.0)
    b = CRDTDocument()
    b.entities.set("k", "from_b", node_id="b", ts=2.0)

    ab = merge(a, b)
    ba = merge(b, a)
    assert ab.entities.get("k") == ba.entities.get("k")


def test_crdt_doc_merge_associative():
    """merge(merge(A, B), C) == merge(A, merge(B, C))."""
    a = CRDTDocument()
    a.chunks.add("x", node_id="a")
    b = CRDTDocument()
    b.chunks.add("y", node_id="b")
    c = CRDTDocument()
    c.chunks.add("z", node_id="c")

    left = merge(merge(a, b), c)
    right = merge(a, merge(b, c))
    assert set(left.chunks.elements()) == set(right.chunks.elements())


def test_crdt_doc_roundtrip_dict():
    doc = CRDTDocument()
    doc.chunks.add("c1", node_id="n1")
    doc.entities.set("k", "v", node_id="n1")
    doc2 = CRDTDocument.from_dict(doc.to_dict())
    assert doc2.chunks.contains("c1")
    assert doc2.entities.get("k") == "v"


# ---------------------------------------------------------------------------
# RelayStub
# ---------------------------------------------------------------------------

def test_relay_encode_decode_roundtrip():
    doc = CRDTDocument()
    doc.chunks.add("c1", node_id="n1")
    doc.entities.set("x", "hello", node_id="n1")
    relay = RelayStub()
    payload = relay.encode(doc)
    doc2 = relay.decode(payload)
    assert doc2.chunks.contains("c1")
    assert doc2.entities.get("x") == "hello"


def test_relay_push_returns_bytes():
    doc = CRDTDocument()
    relay = RelayStub()
    payload = relay.push(doc)
    assert isinstance(payload, bytes)


def test_relay_pull_returns_document():
    doc = CRDTDocument()
    doc.chunks.add("chunk-abc", node_id="phone")
    relay = RelayStub()
    payload = relay.push(doc)
    received = relay.pull(payload)
    assert received.chunks.contains("chunk-abc")


def test_relay_full_sync_cycle():
    """Phone writes chunk; laptop pulls; conflict resolves deterministically."""
    phone = CRDTDocument()
    phone.chunks.add("c1", node_id="phone")
    phone.entities.set("location", "gym", node_id="phone", ts=1.0)

    laptop = CRDTDocument()
    laptop.chunks.add("c2", node_id="laptop")
    laptop.entities.set("location", "home", node_id="laptop", ts=2.0)

    relay = RelayStub()
    phone_payload = relay.push(phone)
    laptop_payload = relay.push(laptop)

    phone.merge(relay.pull(laptop_payload))
    laptop.merge(relay.pull(phone_payload))

    # Both sides converge
    assert phone.chunks.contains("c1") and phone.chunks.contains("c2")
    assert laptop.chunks.contains("c1") and laptop.chunks.contains("c2")
    # laptop's location wins (higher ts)
    assert phone.entities.get("location") == "home"
    assert laptop.entities.get("location") == "home"
