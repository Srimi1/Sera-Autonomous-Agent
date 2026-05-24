"""Tests for sera.safety.vault + VaultApprovalGate (P-64).

Verification: approve once → same shape auto-approves; deny → 24h cooldown.
Plus the real outclass — encryption + tamper-evidence: the vault file can't be
hand-edited to whitelist a dangerous call.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from sera.safety.approval import VaultApprovalGate
from sera.safety.vault import (
    DEFAULT_DENY_COOLDOWN_S,
    EncryptedVault,
    VaultError,
    fingerprint,
)
from sera.tools.base import ToolCall

# A deterministic 32-byte test key (never touches the real keychain).
_TEST_KEY = b"0123456789abcdef0123456789abcdef"


def _vault(tmp_path: Path, *, clock=None) -> EncryptedVault:
    return EncryptedVault(path=tmp_path / "vault.enc", key=_TEST_KEY, clock=clock or __import__("time").time)


def _call(name: str, args: dict) -> ToolCall:
    return ToolCall(id="c1", name=name, arguments=args)


# ---------------------------------------------------------------------------
# Fingerprint
# ---------------------------------------------------------------------------

class TestFingerprint:
    def test_same_args_same_fp(self) -> None:
        assert fingerprint("shell_run", {"cmd": "ls"}) == fingerprint("shell_run", {"cmd": "ls"})

    def test_key_order_irrelevant(self) -> None:
        a = fingerprint("t", {"x": 1, "y": 2})
        b = fingerprint("t", {"y": 2, "x": 1})
        assert a == b

    def test_different_value_different_fp(self) -> None:
        assert fingerprint("shell_run", {"cmd": "ls"}) != fingerprint("shell_run", {"cmd": "rm -rf /"})

    def test_different_tool_different_fp(self) -> None:
        assert fingerprint("a", {"x": 1}) != fingerprint("b", {"x": 1})


# ---------------------------------------------------------------------------
# Encryption + tamper evidence (the outclass)
# ---------------------------------------------------------------------------

class TestEncryption:
    def test_secret_round_trip(self, tmp_path: Path) -> None:
        v = _vault(tmp_path)
        v.put_secret("anthropic_api_key", "sk-ant-123")
        assert v.get_secret("anthropic_api_key") == "sk-ant-123"

    def test_file_is_not_plaintext(self, tmp_path: Path) -> None:
        v = _vault(tmp_path)
        v.put_secret("token", "SUPERSECRETVALUE")
        raw = (tmp_path / "vault.enc").read_bytes()
        assert b"SUPERSECRETVALUE" not in raw
        assert b"token" not in raw

    def test_wrong_key_cannot_open(self, tmp_path: Path) -> None:
        v = _vault(tmp_path)
        v.put_secret("k", "v")
        other = EncryptedVault(path=tmp_path / "vault.enc", key=b"f" * 32)
        with pytest.raises(VaultError):
            other.get_secret("k")

    def test_tampering_detected(self, tmp_path: Path) -> None:
        """Flip a byte in the ciphertext → GCM auth fails → VaultError."""
        v = _vault(tmp_path)
        v.put_secret("k", "v")
        path = tmp_path / "vault.enc"
        raw = bytearray(path.read_bytes())
        raw[-1] ^= 0x01            # corrupt the GCM tag
        path.write_bytes(bytes(raw))
        with pytest.raises(VaultError):
            v.get_secret("k")

    def test_persists_across_reopen(self, tmp_path: Path) -> None:
        v1 = _vault(tmp_path)
        v1.put_secret("k", "v")
        v2 = EncryptedVault(path=tmp_path / "vault.enc", key=_TEST_KEY)
        assert v2.get_secret("k") == "v"

    def test_unknown_format_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "vault.enc"
        path.write_bytes(b"NOTSERA" + os.urandom(32))
        with pytest.raises(VaultError):
            _vault(tmp_path).get_secret("k")

    def test_delete_and_list_secrets(self, tmp_path: Path) -> None:
        v = _vault(tmp_path)
        v.put_secret("a", "1")
        v.put_secret("b", "2")
        assert v.list_secret_names() == ["a", "b"]
        v.delete_secret("a")
        assert v.list_secret_names() == ["b"]


# ---------------------------------------------------------------------------
# Approval shape-memory
# ---------------------------------------------------------------------------

class TestApprovalMemory:
    def test_allow_remembered_persistently(self, tmp_path: Path) -> None:
        t = [1000.0]
        v = _vault(tmp_path, clock=lambda: t[0])
        v.remember_approval("shell_run", {"cmd": "ls"}, decision=True)
        t[0] = 1000.0 + 10 * 24 * 3600      # 10 days later
        rec = v.check_approval("shell_run", {"cmd": "ls"})
        assert rec is not None and rec.decision is True

    def test_deny_within_cooldown(self, tmp_path: Path) -> None:
        t = [1000.0]
        v = _vault(tmp_path, clock=lambda: t[0])
        v.remember_approval("shell_run", {"cmd": "rm -rf /"}, decision=False)
        t[0] = 1000.0 + 23 * 3600           # 23h later — still in cooldown
        rec = v.check_approval("shell_run", {"cmd": "rm -rf /"})
        assert rec is not None and rec.decision is False

    def test_deny_cooldown_expires(self, tmp_path: Path) -> None:
        t = [1000.0]
        v = _vault(tmp_path, clock=lambda: t[0])
        v.remember_approval("shell_run", {"cmd": "rm -rf /"}, decision=False)
        t[0] = 1000.0 + 25 * 3600           # 25h later — cooldown elapsed
        assert v.check_approval("shell_run", {"cmd": "rm -rf /"}) is None

    def test_allow_ttl_expires(self, tmp_path: Path) -> None:
        t = [1000.0]
        v = _vault(tmp_path, clock=lambda: t[0])
        v.remember_approval("t", {"x": 1}, decision=True, ttl_s=60)
        t[0] = 1000.0 + 61
        assert v.check_approval("t", {"x": 1}) is None

    def test_unknown_shape_returns_none(self, tmp_path: Path) -> None:
        v = _vault(tmp_path)
        assert v.check_approval("t", {"x": 1}) is None

    def test_different_shape_not_matched(self, tmp_path: Path) -> None:
        v = _vault(tmp_path)
        v.remember_approval("shell_run", {"cmd": "ls"}, decision=True)
        assert v.check_approval("shell_run", {"cmd": "rm -rf /"}) is None

    def test_forget_approval(self, tmp_path: Path) -> None:
        v = _vault(tmp_path)
        v.remember_approval("t", {"x": 1}, decision=True)
        v.forget_approval("t", {"x": 1})
        assert v.check_approval("t", {"x": 1}) is None

    def test_clear_approvals(self, tmp_path: Path) -> None:
        v = _vault(tmp_path)
        v.remember_approval("a", {"x": 1}, decision=True)
        v.remember_approval("b", {"y": 2}, decision=False)
        assert v.clear_approvals() == 2
        assert v.check_approval("a", {"x": 1}) is None

    def test_approval_memory_is_encrypted(self, tmp_path: Path) -> None:
        v = _vault(tmp_path)
        v.remember_approval("shell_run", {"cmd": "deploy-prod"}, decision=True)
        raw = (tmp_path / "vault.enc").read_bytes()
        assert b"deploy-prod" not in raw
        assert b"shell_run" not in raw


# ---------------------------------------------------------------------------
# VaultApprovalGate — the end-to-end verification
# ---------------------------------------------------------------------------

class _CountingInner:
    """Inner gate that records how often it was actually prompted."""

    def __init__(self, answer: bool) -> None:
        self.answer = answer
        self.prompts = 0

    async def request(self, call: ToolCall, reason: str = "") -> bool:
        self.prompts += 1
        return self.answer


class TestVaultApprovalGate:
    def test_approve_once_then_auto_approves(self, tmp_path: Path) -> None:
        """Verification: approve once → same shape auto-approves."""
        v = _vault(tmp_path)
        inner = _CountingInner(answer=True)
        gate = VaultApprovalGate(inner=inner, vault=v)
        call = _call("shell_run", {"cmd": "git status"})

        first = asyncio.run(gate.request(call))
        second = asyncio.run(gate.request(call))

        assert first is True and second is True
        assert inner.prompts == 1, "the second identical call must NOT prompt — shape-memory auto-approves"

    def test_deny_arms_24h_cooldown(self, tmp_path: Path) -> None:
        """Verification: deny → 24h cooldown (auto-deny, no re-prompt within 24h)."""
        t = [1000.0]
        v = _vault(tmp_path, clock=lambda: t[0])
        inner = _CountingInner(answer=False)
        gate = VaultApprovalGate(inner=inner, vault=v)
        call = _call("shell_run", {"cmd": "rm -rf /tmp/x"})

        first = asyncio.run(gate.request(call))
        t[0] = 1000.0 + 12 * 3600          # 12h later, within cooldown
        second = asyncio.run(gate.request(call))

        assert first is False and second is False
        assert inner.prompts == 1, "within the 24h cooldown the same shape must auto-deny"

    def test_cooldown_expiry_reprompts(self, tmp_path: Path) -> None:
        t = [1000.0]
        v = _vault(tmp_path, clock=lambda: t[0])
        inner = _CountingInner(answer=False)
        gate = VaultApprovalGate(inner=inner, vault=v)
        call = _call("shell_run", {"cmd": "rm -rf /tmp/x"})

        asyncio.run(gate.request(call))
        t[0] = 1000.0 + DEFAULT_DENY_COOLDOWN_S + 1    # just past 24h
        asyncio.run(gate.request(call))
        assert inner.prompts == 2, "after the cooldown elapses, the gate must prompt again"

    def test_different_shape_prompts_again(self, tmp_path: Path) -> None:
        v = _vault(tmp_path)
        inner = _CountingInner(answer=True)
        gate = VaultApprovalGate(inner=inner, vault=v)

        asyncio.run(gate.request(_call("shell_run", {"cmd": "git status"})))
        asyncio.run(gate.request(_call("shell_run", {"cmd": "git push"})))
        assert inner.prompts == 2, "a different arg-shape is a different decision — must prompt"

    def test_allow_decision_survives_new_gate(self, tmp_path: Path) -> None:
        """The memory is on disk (encrypted) — a fresh gate honors it."""
        v1 = _vault(tmp_path)
        inner1 = _CountingInner(answer=True)
        asyncio.run(VaultApprovalGate(inner=inner1, vault=v1).request(_call("t", {"x": 1})))

        v2 = EncryptedVault(path=tmp_path / "vault.enc", key=_TEST_KEY)
        inner2 = _CountingInner(answer=True)
        result = asyncio.run(VaultApprovalGate(inner=inner2, vault=v2).request(_call("t", {"x": 1})))
        assert result is True
        assert inner2.prompts == 0, "persisted allow must auto-approve in a brand-new process"

    def test_remember_allow_disabled(self, tmp_path: Path) -> None:
        v = _vault(tmp_path)
        inner = _CountingInner(answer=True)
        gate = VaultApprovalGate(inner=inner, vault=v, remember_allow=False)
        call = _call("t", {"x": 1})
        asyncio.run(gate.request(call))
        asyncio.run(gate.request(call))
        assert inner.prompts == 2, "with remember_allow off, every allow prompts"
