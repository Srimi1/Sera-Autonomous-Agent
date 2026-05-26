"""Tests for Discord Ed25519 Interactions-Endpoint signature verification.

P-53.5: a real Discord app 401s at registration unless every interaction
webhook's Ed25519 signature over (timestamp + raw_body) is verified. This
suite proves valid signatures pass, tampered/replayed/forged ones fail, and
the gateway server rejects bad signatures with 401 before parsing.
"""
from __future__ import annotations

import asyncio
import json
import time
import urllib.error
import urllib.request

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from sera.gateway.platforms.discord import (
    DiscordSignatureVerifier,
    verify_discord_signature,
)
from sera.gateway.server import build_server


# ---------------------------------------------------------------------------
# Key helpers — real Ed25519 keypairs, no hardcoded fixtures
# ---------------------------------------------------------------------------

def _keypair() -> tuple[Ed25519PrivateKey, str]:
    priv = Ed25519PrivateKey.generate()
    pub_hex = priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    ).hex()
    return priv, pub_hex


def _sign(priv: Ed25519PrivateKey, timestamp: str, body: bytes) -> str:
    return priv.sign(timestamp.encode("utf-8") + body).hex()


# ---------------------------------------------------------------------------
# verify_discord_signature — core function
# ---------------------------------------------------------------------------

class TestVerifyFunction:
    def test_valid_signature_passes(self) -> None:
        priv, pub = _keypair()
        ts = str(int(time.time()))
        body = b'{"type":1}'
        sig = _sign(priv, ts, body)
        assert verify_discord_signature(pub, sig, ts, body) is True

    def test_tampered_body_fails(self) -> None:
        priv, pub = _keypair()
        ts = str(int(time.time()))
        sig = _sign(priv, ts, b'{"type":1}')
        assert verify_discord_signature(pub, sig, ts, b'{"type":2}') is False

    def test_tampered_timestamp_fails(self) -> None:
        priv, pub = _keypair()
        ts = str(int(time.time()))
        body = b'{"type":1}'
        sig = _sign(priv, ts, body)
        wrong_ts = str(int(time.time()) + 1)
        assert verify_discord_signature(pub, sig, wrong_ts, body) is False

    def test_wrong_public_key_fails(self) -> None:
        priv, _ = _keypair()
        _, other_pub = _keypair()
        ts = str(int(time.time()))
        body = b'{"type":1}'
        sig = _sign(priv, ts, body)
        assert verify_discord_signature(other_pub, sig, ts, body) is False

    def test_stale_timestamp_rejected_for_replay(self) -> None:
        """A cryptographically valid signature is still rejected if too old."""
        priv, pub = _keypair()
        old_ts = str(int(time.time()) - 10_000)  # ~2.7h ago
        body = b'{"type":1}'
        sig = _sign(priv, old_ts, body)
        assert verify_discord_signature(pub, sig, old_ts, body, max_age_s=300) is False

    def test_fresh_timestamp_within_window_passes(self) -> None:
        priv, pub = _keypair()
        ts = str(int(time.time()) - 60)  # 1 min ago, within 5-min window
        body = b'{"type":1}'
        sig = _sign(priv, ts, body)
        assert verify_discord_signature(pub, sig, ts, body, max_age_s=300) is True

    def test_age_check_disabled_accepts_old_fixture(self) -> None:
        priv, pub = _keypair()
        old_ts = "1000000"  # ancient, but max_age_s=None skips the check
        body = b'{"type":1}'
        sig = _sign(priv, old_ts, body)
        assert verify_discord_signature(pub, sig, old_ts, body, max_age_s=None) is True

    def test_non_numeric_timestamp_fails_age_check(self) -> None:
        priv, pub = _keypair()
        body = b'{"type":1}'
        sig = _sign(priv, "not-a-number", body)
        assert verify_discord_signature(pub, sig, "not-a-number", body) is False

    def test_empty_inputs_fail(self) -> None:
        _, pub = _keypair()
        assert verify_discord_signature(pub, "", "123", b"x") is False
        assert verify_discord_signature("", "ab", "123", b"x") is False
        assert verify_discord_signature(pub, "ab", "", b"x") is False

    def test_malformed_hex_fails(self) -> None:
        _, pub = _keypair()
        ts = str(int(time.time()))
        assert verify_discord_signature(pub, "zzzz", ts, b"x") is False

    def test_clock_injection_controls_freshness(self) -> None:
        priv, pub = _keypair()
        ts = "1000"
        body = b'{"type":1}'
        sig = _sign(priv, ts, body)
        # Pin clock just inside the window → passes.
        assert verify_discord_signature(
            pub, sig, ts, body, max_age_s=300, clock=lambda: 1200.0
        ) is True
        # Pin clock outside the window → fails.
        assert verify_discord_signature(
            pub, sig, ts, body, max_age_s=300, clock=lambda: 2000.0
        ) is False


# ---------------------------------------------------------------------------
# DiscordSignatureVerifier — server-pluggable callable
# ---------------------------------------------------------------------------

class TestVerifierCallable:
    def test_valid_headers_pass(self) -> None:
        priv, pub = _keypair()
        ts = str(int(time.time()))
        body = b'{"type":1}'
        sig = _sign(priv, ts, body)
        verifier = DiscordSignatureVerifier(pub)
        headers = {"X-Signature-Ed25519": sig, "X-Signature-Timestamp": ts}
        assert verifier(headers, body) is True

    def test_case_insensitive_headers(self) -> None:
        priv, pub = _keypair()
        ts = str(int(time.time()))
        body = b'{"type":1}'
        sig = _sign(priv, ts, body)
        verifier = DiscordSignatureVerifier(pub)
        headers = {"x-signature-ed25519": sig, "x-signature-timestamp": ts}
        assert verifier(headers, body) is True

    def test_missing_headers_fail(self) -> None:
        _, pub = _keypair()
        verifier = DiscordSignatureVerifier(pub)
        assert verifier({}, b'{"type":1}') is False

    def test_only_signature_no_timestamp_fails(self) -> None:
        priv, pub = _keypair()
        ts = str(int(time.time()))
        body = b'{"type":1}'
        sig = _sign(priv, ts, body)
        verifier = DiscordSignatureVerifier(pub)
        assert verifier({"X-Signature-Ed25519": sig}, body) is False


# ---------------------------------------------------------------------------
# Gateway server integration — 401 before parse
# ---------------------------------------------------------------------------

def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _post_signed(url: str, body: bytes, sig: str, ts: str) -> int:
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Signature-Ed25519": sig,
            "X-Signature-Timestamp": ts,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code


class TestServerEnforcement:
    def test_valid_signature_accepted_202(self) -> None:
        priv, pub = _keypair()

        async def _go():
            server, _q = build_server(
                port=0, verifiers={"discord": DiscordSignatureVerifier(pub)},
            )
            server.start()
            try:
                ts = str(int(time.time()))
                body = json.dumps(
                    {"content": "hi", "author": {"id": "u1"}, "type": 0, "channel_id": "c1"}
                ).encode("utf-8")
                sig = _sign(priv, ts, body)
                return _post_signed(f"{server.url}/webhook/discord", body, sig, ts)
            finally:
                server.stop()

        assert _run(_go()) == 202

    def test_invalid_signature_rejected_401(self) -> None:
        priv, pub = _keypair()

        async def _go():
            server, _q = build_server(
                port=0, verifiers={"discord": DiscordSignatureVerifier(pub)},
            )
            server.start()
            try:
                ts = str(int(time.time()))
                body = b'{"content":"hi","author":{"id":"u1"},"type":0,"channel_id":"c1"}'
                # Sign a DIFFERENT body → signature won't match what we send.
                sig = _sign(priv, ts, b'{"content":"tampered"}')
                return _post_signed(f"{server.url}/webhook/discord", body, sig, ts)
            finally:
                server.stop()

        assert _run(_go()) == 401

    def test_missing_signature_rejected_401(self) -> None:
        _, pub = _keypair()

        async def _go():
            server, _q = build_server(
                port=0, verifiers={"discord": DiscordSignatureVerifier(pub)},
            )
            server.start()
            try:
                req = urllib.request.Request(
                    f"{server.url}/webhook/discord",
                    data=b'{"content":"hi"}',
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                try:
                    with urllib.request.urlopen(req, timeout=3.0) as resp:
                        return resp.status
                except urllib.error.HTTPError as e:
                    return e.code
            finally:
                server.stop()

        assert _run(_go()) == 401

    def test_unverified_platform_still_accepted(self) -> None:
        """A platform with no verifier configured is unaffected (telegram)."""
        _, pub = _keypair()

        async def _go():
            server, _q = build_server(
                port=0, verifiers={"discord": DiscordSignatureVerifier(pub)},
            )
            server.start()
            try:
                body = json.dumps({"text": "hi", "user_id": "u", "chat_id": "c"}).encode()
                req = urllib.request.Request(
                    f"{server.url}/webhook/telegram", data=body,
                    headers={"Content-Type": "application/json"}, method="POST",
                )
                with urllib.request.urlopen(req, timeout=3.0) as resp:
                    return resp.status
            finally:
                server.stop()

        assert _run(_go()) == 202

    def test_unauthorized_counter_increments(self) -> None:
        _, pub = _keypair()

        async def _go():
            server, _q = build_server(
                port=0, verifiers={"discord": DiscordSignatureVerifier(pub)},
            )
            server.start()
            try:
                req = urllib.request.Request(
                    f"{server.url}/webhook/discord", data=b'{"x":1}',
                    headers={"Content-Type": "application/json"}, method="POST",
                )
                try:
                    urllib.request.urlopen(req, timeout=3.0)
                except urllib.error.HTTPError:
                    pass
                return server.stats.unauthorized
            finally:
                server.stop()

        assert _run(_go()) >= 1
