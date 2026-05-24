"""P-63: native OS notifications — shim + injectable runner."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from sera.notifications import (
    NotificationEvent,
    NotificationResult,
    _fire_linux,
    _fire_macos,
    _fire_windows,
    _tray_state_path,
    _write_tray_state,
    fire,
    notify_consolidation,
    notify_injection_detected,
    notify_lora_gain,
)


def _ok_runner(cmd):
    return 0, "", ""


def _fail_runner(cmd):
    return 1, "", "cmd not found"


def _capture_runner(cmds: list):
    def _inner(cmd):
        cmds.append(cmd)
        return 0, "", ""
    return _inner


# ---------------------------------------------------------------------------
# NotificationEvent
# ---------------------------------------------------------------------------

def test_event_defaults():
    ev = NotificationEvent(title="Sera", body="hello")
    assert ev.subtitle == ""
    assert ev.tag == ""


def test_event_fields():
    ev = NotificationEvent(title="T", body="B", subtitle="S", tag="test")
    assert ev.title == "T"
    assert ev.subtitle == "S"


# ---------------------------------------------------------------------------
# macOS backend
# ---------------------------------------------------------------------------

def test_macos_ok():
    ev = NotificationEvent(title="T", body="B")
    result = _fire_macos(ev, _ok_runner)
    assert result.sent
    assert result.backend == "osascript"


def test_macos_with_subtitle():
    cmds = []
    ev = NotificationEvent(title="T", body="B", subtitle="Sub")
    _fire_macos(ev, _capture_runner(cmds))
    assert "subtitle" in cmds[0][-1]


def test_macos_fail():
    ev = NotificationEvent(title="T", body="B")
    result = _fire_macos(ev, _fail_runner)
    assert not result.sent
    assert result.error == "cmd not found"


def test_macos_cmd_uses_osascript():
    cmds = []
    ev = NotificationEvent(title="T", body="B")
    _fire_macos(ev, _capture_runner(cmds))
    assert cmds[0][0] == "osascript"


# ---------------------------------------------------------------------------
# Linux backend
# ---------------------------------------------------------------------------

def test_linux_ok():
    ev = NotificationEvent(title="T", body="B")
    result = _fire_linux(ev, _ok_runner)
    assert result.sent
    assert result.backend == "notify-send"


def test_linux_fail():
    ev = NotificationEvent(title="T", body="B")
    result = _fire_linux(ev, _fail_runner)
    assert not result.sent


def test_linux_cmd_uses_notify_send():
    cmds = []
    ev = NotificationEvent(title="T", body="B")
    _fire_linux(ev, _capture_runner(cmds))
    assert cmds[0][0] == "notify-send"


# ---------------------------------------------------------------------------
# Windows backend
# ---------------------------------------------------------------------------

def test_windows_ok():
    ev = NotificationEvent(title="T", body="B")
    result = _fire_windows(ev, _ok_runner)
    assert result.sent
    assert result.backend == "powershell"


def test_windows_fail():
    ev = NotificationEvent(title="T", body="B")
    result = _fire_windows(ev, _fail_runner)
    assert not result.sent


# ---------------------------------------------------------------------------
# fire() dispatch
# ---------------------------------------------------------------------------

def test_fire_returns_result():
    ev = NotificationEvent(title="T", body="B")
    result = fire(ev, runner=_ok_runner, write_tray=False)
    assert isinstance(result, NotificationResult)


def test_fire_unsupported_os(monkeypatch):
    monkeypatch.setattr("platform.system", lambda: "HaikuOS")
    ev = NotificationEvent(title="T", body="B")
    result = fire(ev, runner=_ok_runner, write_tray=False)
    assert not result.sent
    assert "HaikuOS" in result.error


def test_fire_writes_tray_state(tmp_path, monkeypatch):
    tray_file = tmp_path / "tray_state.json"
    monkeypatch.setattr("sera.notifications._tray_state_path", lambda: tray_file)
    ev = NotificationEvent(title="Sera", body="hello", tag="test")
    fire(ev, runner=_ok_runner, write_tray=True)
    state = json.loads(tray_file.read_text())
    assert state["title"] == "Sera"
    assert state["tag"] == "test"


def test_fire_write_tray_false_skips(tmp_path, monkeypatch):
    tray_file = tmp_path / "tray_state.json"
    monkeypatch.setattr("sera.notifications._tray_state_path", lambda: tray_file)
    ev = NotificationEvent(title="T", body="B")
    fire(ev, runner=_ok_runner, write_tray=False)
    assert not tray_file.exists()


# ---------------------------------------------------------------------------
# Loop-event helpers
# ---------------------------------------------------------------------------

def test_notify_consolidation():
    result = notify_consolidation(42, runner=_ok_runner)
    assert isinstance(result, NotificationResult)


def test_notify_consolidation_body_contains_count():
    cmds = []
    notify_consolidation(99, runner=_capture_runner(cmds))
    joined = " ".join(cmds[0])
    assert "99" in joined


def test_notify_lora_gain():
    result = notify_lora_gain(0.031, runner=_ok_runner)
    assert isinstance(result, NotificationResult)


def test_notify_lora_gain_body_contains_value():
    cmds = []
    notify_lora_gain(0.031, runner=_capture_runner(cmds))
    joined = " ".join(cmds[0])
    assert "0.0310" in joined


def test_notify_injection_detected():
    result = notify_injection_detected("role switch", runner=_ok_runner)
    assert isinstance(result, NotificationResult)


def test_notify_injection_body_contains_detail():
    cmds = []
    notify_injection_detected("nested prompt", runner=_capture_runner(cmds))
    joined = " ".join(cmds[0])
    assert "nested prompt" in joined


# ---------------------------------------------------------------------------
# Tray state file
# ---------------------------------------------------------------------------

def test_write_tray_state(tmp_path, monkeypatch):
    tray_file = tmp_path / "tray_state.json"
    monkeypatch.setattr("sera.notifications._tray_state_path", lambda: tray_file)
    ev = NotificationEvent(title="Sera Memory", body="Done", tag="consolidation")
    _write_tray_state(ev)
    data = json.loads(tray_file.read_text())
    assert data["body"] == "Done"
    assert data["tag"] == "consolidation"
