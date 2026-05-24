"""P-97: kernel-level integration — LaunchAgent / systemd / Task Scheduler."""
from __future__ import annotations

import platform
from pathlib import Path

import pytest

from sera.system.helper import (
    HelperStatus,
    _find_sera_bin,
    _launchagent_dest,
    _systemd_dest,
    install,
    status,
    uninstall,
    _HELPER_DIR,
    _PLIST_NAME,
    _SERVICE_NAME,
    _TASK_XML,
)


# ---------------------------------------------------------------------------
# Template files exist
# ---------------------------------------------------------------------------

def test_plist_template_exists():
    assert (_HELPER_DIR / _PLIST_NAME).is_file()


def test_service_template_exists():
    assert (_HELPER_DIR / _SERVICE_NAME).is_file()


def test_task_xml_template_exists():
    assert (_HELPER_DIR / _TASK_XML).is_file()


def test_plist_contains_placeholders():
    src = (_HELPER_DIR / _PLIST_NAME).read_text()
    assert "__SERA_BIN__" in src
    assert "__SERA_HOME__" in src


def test_service_contains_placeholders():
    src = (_HELPER_DIR / _SERVICE_NAME).read_text()
    assert "__SERA_BIN__" in src
    assert "__SERA_HOME__" in src


def test_task_xml_contains_placeholders():
    src = (_HELPER_DIR / _TASK_XML).read_text()
    assert "__SERA_BIN__" in src


# ---------------------------------------------------------------------------
# HelperStatus shape
# ---------------------------------------------------------------------------

def test_helper_status_dataclass():
    s = HelperStatus(platform="Darwin", installed=True, detail="ok")
    assert s.installed
    assert s.platform == "Darwin"


# ---------------------------------------------------------------------------
# macOS LaunchAgent (mocked runner)
# ---------------------------------------------------------------------------

def test_install_launchagent_writes_plist(tmp_path: Path, monkeypatch):
    from sera.system import helper as h
    monkeypatch.setattr(h, "_launchagent_dest", lambda: tmp_path / _PLIST_NAME)
    calls: list[list[str]] = []

    def fake_runner(cmd):
        calls.append(cmd)
        return 0

    result = h._install_launchagent("/usr/local/bin/sera", str(tmp_path), fake_runner)
    assert (tmp_path / _PLIST_NAME).exists()
    plist_content = (tmp_path / _PLIST_NAME).read_text()
    assert "/usr/local/bin/sera" in plist_content
    assert str(tmp_path) in plist_content
    assert "__SERA_BIN__" not in plist_content
    assert result.installed
    assert any("launchctl" in " ".join(c) for c in calls)


def test_uninstall_launchagent_removes_plist(tmp_path: Path, monkeypatch):
    from sera.system import helper as h
    plist_path = tmp_path / _PLIST_NAME
    plist_path.write_text("<plist/>")
    monkeypatch.setattr(h, "_launchagent_dest", lambda: plist_path)
    h._uninstall_launchagent(lambda cmd: 0)
    assert not plist_path.exists()


# ---------------------------------------------------------------------------
# Linux systemd (mocked runner)
# ---------------------------------------------------------------------------

def test_install_systemd_writes_unit(tmp_path: Path, monkeypatch):
    from sera.system import helper as h
    unit_path = tmp_path / _SERVICE_NAME
    monkeypatch.setattr(h, "_systemd_dest", lambda: unit_path)
    calls: list[list[str]] = []

    def fake_runner(cmd):
        calls.append(cmd)
        return 0

    result = h._install_systemd("/usr/bin/sera", str(tmp_path), fake_runner)
    assert unit_path.exists()
    unit_content = unit_path.read_text()
    assert "/usr/bin/sera" in unit_content
    assert "__SERA_BIN__" not in unit_content
    assert result.installed
    assert any("systemctl" in " ".join(c) for c in calls)


def test_uninstall_systemd_removes_unit(tmp_path: Path, monkeypatch):
    from sera.system import helper as h
    unit_path = tmp_path / _SERVICE_NAME
    unit_path.write_text("[Unit]")
    monkeypatch.setattr(h, "_systemd_dest", lambda: unit_path)
    h._uninstall_systemd(lambda cmd: 0)
    assert not unit_path.exists()


# ---------------------------------------------------------------------------
# install / uninstall dispatch (mocked subprocess, avoid real system changes)
# ---------------------------------------------------------------------------

def test_install_dispatches_to_current_platform(tmp_path: Path, monkeypatch):
    from sera.system import helper as h
    results: list[HelperStatus] = []

    def fake_install_la(bin_path, home_path, runner):
        s = HelperStatus("Darwin", True, "mocked")
        results.append(s)
        return s

    def fake_install_sd(bin_path, home_path, runner):
        s = HelperStatus("Linux", True, "mocked")
        results.append(s)
        return s

    monkeypatch.setattr(h, "_install_launchagent", fake_install_la)
    monkeypatch.setattr(h, "_install_systemd", fake_install_sd)
    monkeypatch.setattr(h, "_launchagent_dest", lambda: tmp_path / "fake.plist")
    monkeypatch.setattr(h, "_systemd_dest", lambda: tmp_path / "fake.service")

    result = install(sera_bin="/usr/bin/sera", sera_home=str(tmp_path),
                     runner=lambda cmd: 0)
    assert result.installed
    assert len(results) == 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def test_cli_helper_help():
    from click.testing import CliRunner
    from sera.cli.main import main
    runner = CliRunner()
    result = runner.invoke(main, ["helper", "--help"])
    assert result.exit_code == 0
    assert "install" in result.output
    assert "uninstall" in result.output
    assert "status" in result.output


def test_find_sera_bin_returns_string():
    result = _find_sera_bin()
    assert isinstance(result, str)
    assert len(result) > 0
