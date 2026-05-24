"""P-95: Sera Mobile — shared-core sync client + Tauri scaffold."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from sera.mobile import MobileSyncClient, TransportError
from sera.sync.crdt import CRDTDocument, RelayStub

MOBILE_DIR = Path(__file__).parents[1] / "sera-mobile"


# ---------------------------------------------------------------------------
# Tauri scaffold
# ---------------------------------------------------------------------------

def test_tauri_conf_exists():
    assert (MOBILE_DIR / "tauri.conf.json").is_file()


def test_tauri_conf_valid_json():
    data = json.loads((MOBILE_DIR / "tauri.conf.json").read_text())
    assert data["identifier"] == "com.sera.mobile"


def test_tauri_targets_mobile():
    data = json.loads((MOBILE_DIR / "tauri.conf.json").read_text())
    targets = data["bundle"]["targets"]
    assert "apk" in targets  # Android
    assert "ipa" in targets  # iOS


def test_tauri_crdt_sync_enabled():
    data = json.loads((MOBILE_DIR / "tauri.conf.json").read_text())
    assert data["plugins"]["sera-core"]["crdtSync"]["enabled"] is True


# ---------------------------------------------------------------------------
# Fake transport
# ---------------------------------------------------------------------------

class FakeSidecar:
    """In-memory stand-in for the sidecar; holds a server-side CRDT doc."""

    def __init__(self):
        self.calls = []
        self.server_doc = CRDTDocument()
        self._relay = RelayStub()

    def transport(self, method, url, payload, headers):
        self.calls.append((method, url, payload, headers))
        if url.endswith("/v1/turn"):
            return {"reply": f"echo: {payload['text']}"}
        if url.endswith("/v1/ingest"):
            return {"chunk_id": "chunk-42"}
        if url.endswith("/v1/sync"):
            incoming = self._relay.pull(payload["crdt"].encode())
            self.server_doc.merge(incoming)
            return {"crdt": self._relay.push(self.server_doc).decode()}
        return {"error": "unknown path"}


@pytest.fixture
def sidecar():
    return FakeSidecar()


@pytest.fixture
def client(sidecar):
    return MobileSyncClient(
        server_url="http://127.0.0.1:11111",
        token="test-token",
        transport=sidecar.transport,
    )


# ---------------------------------------------------------------------------
# Core API
# ---------------------------------------------------------------------------

def test_chat(client):
    assert client.chat("hello") == "echo: hello"


def test_chat_sends_bearer_token(client, sidecar):
    client.chat("hi")
    _, _, _, headers = sidecar.calls[0]
    assert headers["Authorization"] == "Bearer test-token"


def test_ingest_returns_chunk_id(client):
    assert client.ingest("page text") == "chunk-42"


def test_call_raises_on_error(client):
    with pytest.raises(TransportError):
        client._call("POST", "/v1/nonsense", {})


# ---------------------------------------------------------------------------
# Offline CRDT
# ---------------------------------------------------------------------------

def test_remember_chunk_local(client):
    client.remember_chunk("c1")
    assert client.doc.chunks.contains("c1")


def test_set_entity_local(client):
    client.set_entity("mood", "focused")
    assert client.doc.entities.get("mood") == "focused"


def test_sync_pushes_and_merges(client, sidecar):
    client.remember_chunk("phone-chunk")
    client.sync()
    # server now has the phone's chunk
    assert sidecar.server_doc.chunks.contains("phone-chunk")


def test_sync_pulls_remote_state(client, sidecar):
    # seed server with a chunk written elsewhere (e.g. laptop)
    sidecar.server_doc.chunks.add("laptop-chunk", node_id="laptop")
    client.remember_chunk("phone-chunk")
    client.sync()
    # phone converges with laptop's write
    assert client.doc.chunks.contains("laptop-chunk")
    assert client.doc.chunks.contains("phone-chunk")


def test_sync_offline_raises(client):
    client.online = False
    with pytest.raises(TransportError):
        client.sync()


def test_offline_edits_survive_until_sync(client, sidecar):
    """Write offline, go online, sync — edit reaches the shared core."""
    client.online = False
    client.remember_chunk("offline-note")
    # cannot sync yet
    with pytest.raises(TransportError):
        client.sync()
    # reconnect
    client.online = True
    client.sync()
    assert sidecar.server_doc.chunks.contains("offline-note")


def test_concurrent_phone_laptop_entity_conflict(client, sidecar):
    """Phone and laptop set the same entity; later ts wins deterministically."""
    sidecar.server_doc.entities.set("location", "office", node_id="laptop", ts=10.0)
    client.doc.entities.set("location", "home", node_id="phone", ts=20.0)
    client.sync()
    assert client.doc.entities.get("location") == "home"
