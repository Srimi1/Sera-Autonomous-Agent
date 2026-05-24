"""Tests for vault key rotation — P-85."""
from __future__ import annotations

from pathlib import Path

import pytest

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sera.safety.vault import EncryptedVault, VaultError


def _key() -> bytes:
    return AESGCM.generate_key(bit_length=256)


def _vault(tmp_path: Path, key: bytes | None = None) -> EncryptedVault:
    k = key or _key()
    return EncryptedVault(path=tmp_path / "vault.enc", key=k)


class TestKeyRotation:
    def test_rotate_preserves_secrets(self, tmp_path: Path) -> None:
        key_a = _key()
        v = _vault(tmp_path, key=key_a)
        v.put_secret("api_key", "sk-secret-123")

        key_b = _key()
        v.rotate_key(key_b)

        assert v.get_secret("api_key") == "sk-secret-123"

    def test_rotate_preserves_approvals(self, tmp_path: Path) -> None:
        key_a = _key()
        v = _vault(tmp_path, key=key_a)
        v.remember_approval("shell_run", {"cmd": "ls"}, decision=True)

        key_b = _key()
        v.rotate_key(key_b)

        rec = v.check_approval("shell_run", {"cmd": "ls"})
        assert rec is not None
        assert rec.decision is True

    def test_old_key_cannot_read_after_rotation(self, tmp_path: Path) -> None:
        path = tmp_path / "vault.enc"
        key_a = _key()
        v = _vault(tmp_path, key=key_a)
        v.put_secret("x", "secret")

        key_b = _key()
        v.rotate_key(key_b)

        # Try reading with the old key
        old_vault = EncryptedVault(path=path, key=key_a)
        with pytest.raises(VaultError):
            old_vault.get_secret("x")

    def test_new_key_instance_reads_rotated_vault(self, tmp_path: Path) -> None:
        path = tmp_path / "vault.enc"
        key_a = _key()
        v = _vault(tmp_path, key=key_a)
        v.put_secret("token", "my-token")

        key_b = _key()
        v.rotate_key(key_b)

        # New instance with key_b reads successfully
        v2 = EncryptedVault(path=path, key=key_b)
        assert v2.get_secret("token") == "my-token"

    def test_invalid_key_length_raises(self, tmp_path: Path) -> None:
        v = _vault(tmp_path)
        with pytest.raises(ValueError, match="must be 32 bytes"):
            v.rotate_key(b"short")

    def test_rotate_multiple_times(self, tmp_path: Path) -> None:
        path = tmp_path / "vault.enc"
        key_a = _key()
        v = _vault(tmp_path, key=key_a)
        v.put_secret("val", "original")

        key_b = _key()
        v.rotate_key(key_b)
        key_c = _key()
        v.rotate_key(key_c)

        v3 = EncryptedVault(path=path, key=key_c)
        assert v3.get_secret("val") == "original"

    def test_rotate_empty_vault(self, tmp_path: Path) -> None:
        key_a = _key()
        v = _vault(tmp_path, key=key_a)
        key_b = _key()
        v.rotate_key(key_b)   # must not raise on empty vault
        assert v.list_secret_names() == []
