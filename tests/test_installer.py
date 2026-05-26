"""P-99: public ship — installer manifest + release workflow validation."""
from __future__ import annotations

import copy
from pathlib import Path

import pytest

from sera.install.manifest import (
    ManifestError,
    Target,
    load_manifest,
    parse_targets,
    validate,
    validate_for_os,
)

ROOT = Path(__file__).parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"


@pytest.fixture
def good_manifest():
    return load_manifest()


# ---------------------------------------------------------------------------
# Files exist
# ---------------------------------------------------------------------------

def test_manifest_file_exists():
    assert (ROOT / "installer" / "manifest.json").is_file()


def test_release_workflow_exists():
    assert WORKFLOW.is_file()


def test_validate_script_exists():
    assert (ROOT / "scripts" / "validate_installer.py").is_file()


# ---------------------------------------------------------------------------
# Real manifest validates
# ---------------------------------------------------------------------------

def test_real_manifest_valid():
    targets = validate()
    assert len(targets) == 3


def test_validate_for_each_os():
    for os_name in ("macos", "windows", "linux"):
        t = validate_for_os(os_name)
        assert t.os == os_name


def test_macos_ships_dmg():
    assert validate_for_os("macos").format == "dmg"


def test_windows_ships_msi():
    assert validate_for_os("windows").format == "msi"


def test_linux_ships_deb():
    assert validate_for_os("linux").format == "deb"


def test_macos_and_windows_require_codesign():
    assert validate_for_os("macos").codesign_required
    assert validate_for_os("windows").codesign_required


def test_linux_does_not_require_codesign():
    assert not validate_for_os("linux").codesign_required


# ---------------------------------------------------------------------------
# Validation failures
# ---------------------------------------------------------------------------

def test_missing_version_fails(good_manifest):
    m = copy.deepcopy(good_manifest)
    del m["version"]
    with pytest.raises(ManifestError):
        validate(m)


def test_budget_over_5min_fails(good_manifest):
    m = copy.deepcopy(good_manifest)
    m["first_reply_budget_seconds"] = 600
    with pytest.raises(ManifestError):
        validate(m)


def test_non_int_budget_fails(good_manifest):
    m = copy.deepcopy(good_manifest)
    m["first_reply_budget_seconds"] = "soon"
    with pytest.raises(ManifestError):
        validate(m)


def test_missing_os_fails(good_manifest):
    m = copy.deepcopy(good_manifest)
    m["targets"] = [t for t in m["targets"] if t["os"] != "linux"]
    with pytest.raises(ManifestError):
        validate(m)


def test_wrong_format_for_os_fails(good_manifest):
    m = copy.deepcopy(good_manifest)
    for t in m["targets"]:
        if t["os"] == "macos":
            t["format"] = "msi"
    with pytest.raises(ManifestError):
        validate(m)


def test_macos_without_codesign_fails(good_manifest):
    m = copy.deepcopy(good_manifest)
    for t in m["targets"]:
        if t["os"] == "macos":
            t["codesign"]["required"] = False
    with pytest.raises(ManifestError):
        validate(m)


def test_empty_arch_fails(good_manifest):
    m = copy.deepcopy(good_manifest)
    for t in m["targets"]:
        if t["os"] == "linux":
            t["arch"] = []
    with pytest.raises(ManifestError):
        validate(m)


def test_validate_for_unknown_os_raises():
    with pytest.raises(ManifestError):
        validate_for_os("haiku")


# ---------------------------------------------------------------------------
# Release workflow
# ---------------------------------------------------------------------------

def test_workflow_covers_three_formats():
    content = WORKFLOW.read_text()
    assert "dmg" in content
    assert "msi" in content
    assert "deb" in content


def test_workflow_has_codesign_steps():
    content = WORKFLOW.read_text()
    assert "Codesign (macOS)" in content
    assert "Codesign (Windows)" in content


def test_workflow_validates_manifest():
    content = WORKFLOW.read_text()
    assert "validate_installer.py" in content


def test_workflow_triggers_on_tags():
    content = WORKFLOW.read_text()
    assert 'tags: ["v*"]' in content


# ---------------------------------------------------------------------------
# parse_targets
# ---------------------------------------------------------------------------

def test_parse_targets_returns_target_objects(good_manifest):
    targets = parse_targets(good_manifest)
    assert all(isinstance(t, Target) for t in targets)
