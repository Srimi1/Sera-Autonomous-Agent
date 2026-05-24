"""P-94: browser extension (Sera-in-tab) — /v1/ingest endpoint + MV3 manifest."""
from __future__ import annotations

import json
import threading
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any

import pytest

EXTENSION_DIR = Path(__file__).parents[1] / "sera-extension"


# ---------------------------------------------------------------------------
# MV3 manifest validation
# ---------------------------------------------------------------------------

def _manifest() -> dict:
    with (EXTENSION_DIR / "manifest.json").open() as f:
        return json.load(f)


def test_manifest_exists():
    assert (EXTENSION_DIR / "manifest.json").is_file()


def test_manifest_version_is_3():
    assert _manifest()["manifest_version"] == 3


def test_manifest_has_active_tab_permission():
    assert "activeTab" in _manifest()["permissions"]


def test_manifest_has_scripting_permission():
    assert "scripting" in _manifest()["permissions"]


def test_manifest_has_localhost_host_permission():
    perms = _manifest().get("host_permissions", [])
    assert any("127.0.0.1" in p for p in perms)


def test_manifest_has_action_popup():
    assert "popup.html" in _manifest()["action"]["default_popup"]


def test_manifest_has_service_worker():
    assert "service_worker" in _manifest()["background"]


def test_popup_html_exists():
    assert (EXTENSION_DIR / "popup.html").is_file()


def test_popup_js_exists():
    assert (EXTENSION_DIR / "popup.js").is_file()


def test_popup_js_calls_ingest_endpoint():
    src = (EXTENSION_DIR / "popup.js").read_text()
    assert "/v1/ingest" in src


def test_popup_js_sends_bearer_auth():
    src = (EXTENSION_DIR / "popup.js").read_text()
    assert "Authorization" in src
    assert "Bearer" in src


def test_popup_js_extracts_innertext():
    src = (EXTENSION_DIR / "popup.js").read_text()
    assert "innerText" in src


def test_background_js_exists():
    assert (EXTENSION_DIR / "background.js").is_file()


# ---------------------------------------------------------------------------
# /v1/ingest HTTP endpoint
# ---------------------------------------------------------------------------

def _http(method: str, url: str, body: dict | None = None, token: str | None = None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _make_server(ingest_fn=None):
    from sera.rpc.http_api import SeraHTTPAPI, SignedBearer

    bearer = SignedBearer(signing_key="test-key-94")
    token = bearer.issue("ext", scopes=["turn"])

    def _stub_turn(payload):
        return {"ok": True, "text": "ok", "profile_used": None,
                "latency_ms": 1, "error": None}

    srv = SeraHTTPAPI(
        host="127.0.0.1",
        port=0,
        turn_fn=_stub_turn,
        bearer=bearer,
        ingest_fn=ingest_fn,
    )
    return srv, token


def _start(srv):
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    time.sleep(0.05)
    return t


def test_ingest_returns_501_when_no_ingest_fn():
    srv, token = _make_server(ingest_fn=None)
    _start(srv)
    status, body = _http("POST", f"http://127.0.0.1:{srv.server_address[1]}/v1/ingest",
                          body={"url": "https://example.com", "content": "hello"},
                          token=token)
    srv.shutdown()
    assert status == 501


def test_ingest_stores_chunk():
    stored: list[dict] = []

    def _ingest(*, url, content, title):
        stored.append({"url": url, "content": content, "title": title})
        return len(stored)

    srv, token = _make_server(ingest_fn=_ingest)
    _start(srv)
    status, body = _http("POST", f"http://127.0.0.1:{srv.server_address[1]}/v1/ingest",
                          body={"url": "https://example.com", "content": "page text", "title": "Example"},
                          token=token)
    srv.shutdown()
    assert status == 200
    assert body["ok"] is True
    assert body["chunk_id"] == 1
    assert stored[0]["url"] == "https://example.com"


def test_ingest_requires_auth():
    def _ingest(*, url, content, title):
        return 1

    srv, _ = _make_server(ingest_fn=_ingest)
    _start(srv)
    status, body = _http("POST", f"http://127.0.0.1:{srv.server_address[1]}/v1/ingest",
                          body={"content": "text"})  # no token
    srv.shutdown()
    assert status == 401


def test_ingest_rejects_empty_content():
    def _ingest(*, url, content, title):
        return 1

    srv, token = _make_server(ingest_fn=_ingest)
    _start(srv)
    status, body = _http("POST", f"http://127.0.0.1:{srv.server_address[1]}/v1/ingest",
                          body={"url": "x", "content": "   "},
                          token=token)
    srv.shutdown()
    assert status == 400


def test_ingest_openapi_spec_includes_endpoint():
    from sera.rpc.http_api import build_openapi_spec
    spec = build_openapi_spec()
    assert "/v1/ingest" in spec["paths"]
    assert "post" in spec["paths"]["/v1/ingest"]
