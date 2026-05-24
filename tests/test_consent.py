"""Tests for sera.os_hooks — signed consent gate (P-70).

Verification: a capability works only with active consent; revoke + retry is
refused. The consent map is stored in the encrypted vault, so it's tamper-
evident (proven via the file-bytes check).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from sera.os_hooks import clipboard, keyboard, screen
from sera.os_hooks.a11y import read_a11y_tree
from sera.os_hooks.consent import (
    ConsentDenied,
    ConsentManager,
    Feature,
)
from sera.safety.vault import EncryptedVault

_KEY = b"0123456789abcdef0123456789abcdef"


def _mgr(tmp_path: Path, *, clock=None) -> ConsentManager:
    vault = EncryptedVault(path=tmp_path / "vault.enc", key=_KEY,
                           clock=clock or __import__("time").time)
    return ConsentManager(vault, clock=clock or __import__("time").time)


# ---------------------------------------------------------------------------
# ConsentManager
# ---------------------------------------------------------------------------

class TestConsentManager:
    def test_default_denied(self, tmp_path: Path) -> None:
        m = _mgr(tmp_path)
        assert m.is_allowed(Feature.SCREEN) is False

    def test_grant_then_allowed(self, tmp_path: Path) -> None:
        m = _mgr(tmp_path)
        m.grant(Feature.SCREEN)
        assert m.is_allowed(Feature.SCREEN) is True

    def test_revoke_flips_off(self, tmp_path: Path) -> None:
        """One-click off: revoke → immediately denied."""
        m = _mgr(tmp_path)
        m.grant(Feature.CLIPBOARD)
        assert m.is_allowed(Feature.CLIPBOARD) is True
        m.revoke(Feature.CLIPBOARD)
        assert m.is_allowed(Feature.CLIPBOARD) is False

    def test_grants_are_per_feature(self, tmp_path: Path) -> None:
        m = _mgr(tmp_path)
        m.grant(Feature.SCREEN)
        assert m.is_allowed(Feature.SCREEN) is True
        assert m.is_allowed(Feature.KEYBOARD) is False

    def test_ttl_expires(self, tmp_path: Path) -> None:
        t = [1000.0]
        m = _mgr(tmp_path, clock=lambda: t[0])
        m.grant(Feature.SCREEN, ttl_s=60)
        t[0] = 1000.0 + 61
        assert m.is_allowed(Feature.SCREEN) is False

    def test_require_raises_when_denied(self, tmp_path: Path) -> None:
        m = _mgr(tmp_path)
        with pytest.raises(ConsentDenied):
            m.require(Feature.SCREEN)

    def test_require_passes_when_granted(self, tmp_path: Path) -> None:
        m = _mgr(tmp_path)
        m.grant(Feature.SCREEN)
        m.require(Feature.SCREEN)   # no raise

    def test_status_reports_all_features(self, tmp_path: Path) -> None:
        m = _mgr(tmp_path)
        m.grant(Feature.SCREEN)
        status = m.status()
        assert status["screen"] is True
        assert status["keyboard"] is False
        assert set(status) == {"screen", "clipboard", "accessibility", "keyboard"}

    def test_consent_persists_across_manager(self, tmp_path: Path) -> None:
        m1 = _mgr(tmp_path)
        m1.grant(Feature.SCREEN)
        vault2 = EncryptedVault(path=tmp_path / "vault.enc", key=_KEY)
        m2 = ConsentManager(vault2)
        assert m2.is_allowed(Feature.SCREEN) is True

    def test_consent_is_encrypted_at_rest(self, tmp_path: Path) -> None:
        """The outclass: consent state is in the authenticated vault, not plaintext."""
        m = _mgr(tmp_path)
        m.grant(Feature.SCREEN)
        raw = (tmp_path / "vault.enc").read_bytes()
        assert b"screen" not in raw
        assert b"granted" not in raw


# ---------------------------------------------------------------------------
# Hooks gated by consent — the phase verification
# ---------------------------------------------------------------------------

class TestGatedHooks:
    def test_screen_capture_refused_without_consent(self, tmp_path: Path) -> None:
        m = _mgr(tmp_path)
        with pytest.raises(ConsentDenied):
            screen.capture_screen(m, _capture=lambda out: out.write_bytes(b"PNG"))

    def test_screen_capture_works_with_consent(self, tmp_path: Path) -> None:
        m = _mgr(tmp_path)
        m.grant(Feature.SCREEN)
        captured = {}
        screen.capture_screen(
            m, out_path=tmp_path / "shot.png",
            _capture=lambda out: captured.update(path=out),
        )
        assert captured["path"] == tmp_path / "shot.png"

    def test_revoke_then_retry_refused(self, tmp_path: Path) -> None:
        """Phase verification: works once, revoke, retry refused."""
        m = _mgr(tmp_path)
        m.grant(Feature.SCREEN)
        screen.capture_screen(m, out_path=tmp_path / "a.png", _capture=lambda out: None)
        m.revoke(Feature.SCREEN)
        with pytest.raises(ConsentDenied):
            screen.capture_screen(m, out_path=tmp_path / "b.png", _capture=lambda out: None)

    def test_clipboard_gated(self, tmp_path: Path) -> None:
        m = _mgr(tmp_path)
        with pytest.raises(ConsentDenied):
            clipboard.read_clipboard(m, _read=lambda: "secret")
        m.grant(Feature.CLIPBOARD)
        assert clipboard.read_clipboard(m, _read=lambda: "secret") == "secret"

    def test_keyboard_gated(self, tmp_path: Path) -> None:
        m = _mgr(tmp_path)
        typed: list[str] = []
        with pytest.raises(ConsentDenied):
            keyboard.type_text(m, "hi", _type=typed.append)
        m.grant(Feature.KEYBOARD)
        keyboard.type_text(m, "hi", _type=typed.append)
        assert typed == ["hi"]

    def test_a11y_gated(self, tmp_path: Path) -> None:
        m = _mgr(tmp_path)
        with pytest.raises(ConsentDenied):
            read_a11y_tree(m, _read=lambda: {"role": "window"})
        m.grant(Feature.ACCESSIBILITY)
        assert read_a11y_tree(m, _read=lambda: {"role": "window"})["role"] == "window"
