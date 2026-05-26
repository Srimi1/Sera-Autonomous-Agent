"""Tests for sera.rpc.http_api — signed bearer, OpenAPI spec, HTTP round-trip.

P-59 verification: `curl POST /v1/turn` round-trips. The E2E test drives a
real asyncio loop + Router behind a real HTTP socket via urllib.
"""
from __future__ import annotations

import asyncio
import json
import threading
import urllib.error
import urllib.request
from typing import AsyncIterator

import pytest

from sera.rpc.http_api import (
    API_VERSION,
    SeraHTTPAPI,
    SignedBearer,
    build_openapi_spec,
    make_async_bridge,
)
from sera.gateway.router import Router
from sera.llm.base import StreamChunk


# ---------------------------------------------------------------------------
# SignedBearer
# ---------------------------------------------------------------------------

class TestSignedBearer:
    def test_issue_then_verify(self) -> None:
        b = SignedBearer(signing_key="k")
        token = b.issue("cli", scopes=["turn"])
        claims = b.verify(token)
        assert claims is not None
        assert claims.sub == "cli"
        assert claims.scopes == ["turn"]

    def test_token_has_three_jwt_parts(self) -> None:
        b = SignedBearer(signing_key="k")
        token = b.issue("cli")
        assert token.count(".") == 2

    def test_wrong_key_rejected(self) -> None:
        token = SignedBearer(signing_key="real").issue("cli", scopes=["turn"])
        assert SignedBearer(signing_key="forged").verify(token) is None

    def test_tampered_payload_rejected(self) -> None:
        b = SignedBearer(signing_key="k")
        token = b.issue("cli", scopes=["turn"])
        header, payload, sig = token.split(".")
        # Flip a character in the payload segment.
        tampered_payload = ("A" if payload[0] != "A" else "B") + payload[1:]
        forged = f"{header}.{tampered_payload}.{sig}"
        assert b.verify(forged) is None

    def test_expired_token_rejected(self) -> None:
        b = SignedBearer(signing_key="k")
        token = b.issue("cli", scopes=["turn"], ttl_s=100, now=1000.0)
        # now=1101 is past exp=1100
        assert b.verify(token, now=1101.0) is None

    def test_unexpired_token_accepted(self) -> None:
        b = SignedBearer(signing_key="k")
        token = b.issue("cli", scopes=["turn"], ttl_s=100, now=1000.0)
        assert b.verify(token, now=1099.0) is not None

    def test_exp_boundary_is_exclusive(self) -> None:
        b = SignedBearer(signing_key="k")
        token = b.issue("cli", ttl_s=100, now=1000.0)
        # Exactly at exp → rejected (now >= exp)
        assert b.verify(token, now=1100.0) is None

    def test_malformed_token_rejected(self) -> None:
        b = SignedBearer(signing_key="k")
        assert b.verify("not-a-jwt") is None
        assert b.verify("a.b") is None
        assert b.verify("") is None
        assert b.verify("a.b.c.d") is None

    def test_garbage_signature_rejected(self) -> None:
        b = SignedBearer(signing_key="k")
        token = b.issue("cli")
        header, payload, _ = token.split(".")
        assert b.verify(f"{header}.{payload}.@@@@@") is None

    def test_empty_key_raises(self) -> None:
        with pytest.raises(ValueError):
            SignedBearer(signing_key="")

    def test_scopes_preserved_multiple(self) -> None:
        b = SignedBearer(signing_key="k")
        token = b.issue("admin", scopes=["turn", "admin", "memory"])
        claims = b.verify(token)
        assert claims is not None
        assert set(claims.scopes) == {"turn", "admin", "memory"}

    def test_iat_and_exp_set(self) -> None:
        b = SignedBearer(signing_key="k")
        token = b.issue("cli", ttl_s=3600, now=5000.0)
        claims = b.verify(token, now=5001.0)
        assert claims is not None
        assert claims.iat == 5000
        assert claims.exp == 8600

    def test_token_is_standard_hs256_jwt(self) -> None:
        """Header decodes to the canonical HS256 JWT header."""
        import base64

        b = SignedBearer(signing_key="k")
        token = b.issue("cli")
        header_b64 = token.split(".")[0]
        pad = "=" * (-len(header_b64) % 4)
        header = json.loads(base64.urlsafe_b64decode(header_b64 + pad))
        assert header == {"alg": "HS256", "typ": "JWT"}


# ---------------------------------------------------------------------------
# build_openapi_spec
# ---------------------------------------------------------------------------

class TestOpenAPISpec:
    def test_is_openapi_31(self) -> None:
        spec = build_openapi_spec()
        assert spec["openapi"] == "3.1.0"

    def test_version_matches(self) -> None:
        spec = build_openapi_spec()
        assert spec["info"]["version"] == API_VERSION

    def test_turn_path_documented(self) -> None:
        spec = build_openapi_spec()
        assert "/v1/turn" in spec["paths"]
        assert "post" in spec["paths"]["/v1/turn"]

    def test_turn_requires_bearer_security(self) -> None:
        spec = build_openapi_spec()
        security = spec["paths"]["/v1/turn"]["post"]["security"]
        assert {"bearerAuth": []} in security

    def test_bearer_security_scheme_is_jwt(self) -> None:
        spec = build_openapi_spec()
        scheme = spec["components"]["securitySchemes"]["bearerAuth"]
        assert scheme["type"] == "http"
        assert scheme["scheme"] == "bearer"
        assert scheme["bearerFormat"] == "JWT"

    def test_request_response_schemas_present(self) -> None:
        spec = build_openapi_spec()
        schemas = spec["components"]["schemas"]
        assert "TurnRequest" in schemas
        assert "TurnResponse" in schemas
        assert "text" in schemas["TurnRequest"]["properties"]

    def test_base_url_injected(self) -> None:
        spec = build_openapi_spec(base_url="http://127.0.0.1:9999")
        assert spec["servers"][0]["url"] == "http://127.0.0.1:9999"

    def test_spec_is_json_serializable(self) -> None:
        spec = build_openapi_spec()
        # Must round-trip through JSON (it's served as application/json).
        assert json.loads(json.dumps(spec)) == spec


# ---------------------------------------------------------------------------
# HTTP layer with a stub turn_fn (auth / routing / errors)
# ---------------------------------------------------------------------------

def _http(method: str, url: str, *, body: dict | None = None, headers: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8") or "{}")


@pytest.fixture
def stub_api():
    """An API whose turn_fn echoes the text — isolates HTTP/auth from the agent."""
    calls: list[dict] = []

    def turn_fn(payload: dict) -> dict:
        text = str(payload.get("text") or "").strip()
        if not text:
            raise ValueError("text is required")
        calls.append(payload)
        return {"ok": True, "text": f"echo: {text}", "profile_used": "stub", "latency_ms": 1, "error": None}

    bearer = SignedBearer(signing_key="test-key")
    api = SeraHTTPAPI(host="127.0.0.1", port=0, turn_fn=turn_fn, bearer=bearer)
    api.start()
    yield api, bearer, calls
    api.stop()


class TestHTTPLayer:
    def test_healthz_public(self, stub_api) -> None:
        api, _, _ = stub_api
        status, body = _http("GET", f"{api.url}/healthz")
        assert status == 200
        assert body["ok"] is True

    def test_openapi_public(self, stub_api) -> None:
        api, _, _ = stub_api
        status, body = _http("GET", f"{api.url}/openapi.json")
        assert status == 200
        assert body["openapi"] == "3.1.0"
        assert "/v1/turn" in body["paths"]

    def test_turn_requires_token(self, stub_api) -> None:
        api, _, _ = stub_api
        status, body = _http("POST", f"{api.url}/v1/turn", body={"text": "hi"})
        assert status == 401

    def test_turn_rejects_forged_token(self, stub_api) -> None:
        api, _, _ = stub_api
        forged = SignedBearer(signing_key="wrong").issue("cli", scopes=["turn"])
        status, _ = _http("POST", f"{api.url}/v1/turn", body={"text": "hi"},
                          headers={"Authorization": f"Bearer {forged}"})
        assert status == 401

    def test_turn_rejects_missing_scope(self, stub_api) -> None:
        api, bearer, _ = stub_api
        token = bearer.issue("cli", scopes=["readonly"])  # no "turn" scope
        status, body = _http("POST", f"{api.url}/v1/turn", body={"text": "hi"},
                             headers={"Authorization": f"Bearer {token}"})
        assert status == 403

    def test_turn_succeeds_with_valid_token(self, stub_api) -> None:
        api, bearer, calls = stub_api
        token = bearer.issue("cli", scopes=["turn"])
        status, body = _http("POST", f"{api.url}/v1/turn", body={"text": "hello"},
                             headers={"Authorization": f"Bearer {token}"})
        assert status == 200
        assert body["text"] == "echo: hello"
        assert len(calls) == 1

    def test_turn_empty_text_400(self, stub_api) -> None:
        api, bearer, _ = stub_api
        token = bearer.issue("cli", scopes=["turn"])
        status, body = _http("POST", f"{api.url}/v1/turn", body={"text": ""},
                             headers={"Authorization": f"Bearer {token}"})
        assert status == 400

    def test_turn_bad_json_400(self, stub_api) -> None:
        api, bearer, _ = stub_api
        token = bearer.issue("cli", scopes=["turn"])
        req = urllib.request.Request(
            f"{api.url}/v1/turn",
            data=b"{not json",
            headers={"Authorization": f"Bearer {token}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                status = resp.status
        except urllib.error.HTTPError as e:
            status = e.code
        assert status == 400

    def test_unknown_post_path_404(self, stub_api) -> None:
        api, bearer, _ = stub_api
        token = bearer.issue("cli", scopes=["turn"])
        status, _ = _http("POST", f"{api.url}/v1/nope", body={"text": "x"},
                          headers={"Authorization": f"Bearer {token}"})
        assert status == 404

    def test_unknown_get_path_404(self, stub_api) -> None:
        api, _, _ = stub_api
        status, _ = _http("GET", f"{api.url}/nope")
        assert status == 404

    def test_stats_increment(self, stub_api) -> None:
        api, bearer, _ = stub_api
        token = bearer.issue("cli", scopes=["turn"])
        _http("POST", f"{api.url}/v1/turn", body={"text": "a"},
              headers={"Authorization": f"Bearer {token}"})
        _http("POST", f"{api.url}/v1/turn", body={"text": "b"})  # no token → 401
        assert api.stats["ok"] == 1
        assert api.stats["unauthorized"] == 1


# ---------------------------------------------------------------------------
# E2E: real asyncio loop + real Router behind a real socket
# ---------------------------------------------------------------------------

class _StubLLM:
    name = "openai"
    context_budget = 32_000
    model = "stub"

    async def stream(self, messages, tools=None, system=None) -> AsyncIterator[StreamChunk]:
        yield StreamChunk(delta_text="agent reply")
        yield StreamChunk(finish_reason="stop")


class TestE2ECurlRoundTrip:
    def test_post_v1_turn_round_trips_through_router(self) -> None:
        """P-59 verification: HTTP POST → bridge → Router.dispatch → run_turn → HTTP 200.

        A real asyncio loop runs in a background thread; the API's worker
        thread submits the coroutine to it via run_coroutine_threadsafe.
        """
        loop = asyncio.new_event_loop()
        loop_thread = threading.Thread(target=loop.run_forever, daemon=True)
        loop_thread.start()

        router = Router(llm_factory=lambda _p: _StubLLM())
        bearer = SignedBearer(signing_key="e2e-key")
        turn_fn = make_async_bridge(loop, router, timeout_s=10.0)
        api = SeraHTTPAPI(host="127.0.0.1", port=0, turn_fn=turn_fn, bearer=bearer)
        api.start()

        try:
            token = bearer.issue("cli", scopes=["turn"])
            status, body = _http(
                "POST", f"{api.url}/v1/turn",
                body={"text": "are you there?"},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert status == 200
            assert body["ok"] is True
            assert body["text"] == "agent reply"
            assert body["latency_ms"] >= 0
        finally:
            api.stop()
            loop.call_soon_threadsafe(loop.stop)
            loop_thread.join(timeout=3.0)
            loop.close()

    def test_openapi_describes_the_live_server(self) -> None:
        """The published spec's server URL matches the bound address."""
        loop = asyncio.new_event_loop()
        loop_thread = threading.Thread(target=loop.run_forever, daemon=True)
        loop_thread.start()

        router = Router(llm_factory=lambda _p: _StubLLM())
        bearer = SignedBearer(signing_key="e2e-key")
        api = SeraHTTPAPI(
            host="127.0.0.1", port=0,
            turn_fn=make_async_bridge(loop, router),
            bearer=bearer,
        )
        api.start()
        try:
            status, spec = _http("GET", f"{api.url}/openapi.json")
            assert status == 200
            assert spec["servers"][0]["url"] == api.url
        finally:
            api.stop()
            loop.call_soon_threadsafe(loop.stop)
            loop_thread.join(timeout=3.0)
            loop.close()
