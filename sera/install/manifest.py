"""P-99: installer manifest validation — DMG / MSI / deb targets + codesign.

Validates `installer/manifest.json` so CI fails loudly if a target loses its
codesign config or the three platforms drift. Mirrors the release workflow's
expectations.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

REQUIRED_OS = {"macos", "windows", "linux"}
FORMAT_FOR_OS = {"macos": "dmg", "windows": "msi", "linux": "deb"}
# Platforms where unsigned binaries get quarantined / SmartScreen-blocked.
CODESIGN_REQUIRED_OS = {"macos", "windows"}


class ManifestError(RuntimeError):
    """Raised when the installer manifest is malformed or incomplete."""


@dataclass
class Target:
    os: str
    format: str
    arch: list[str]
    codesign_required: bool


def _manifest_path() -> Path:
    return Path(__file__).parents[2] / "installer" / "manifest.json"


def load_manifest(path: Path | None = None) -> dict:
    p = path or _manifest_path()
    if not p.is_file():
        raise ManifestError(f"manifest not found: {p}")
    return json.loads(p.read_text())


def parse_targets(manifest: dict) -> list[Target]:
    targets = []
    for t in manifest.get("targets", []):
        targets.append(
            Target(
                os=t["os"],
                format=t["format"],
                arch=list(t.get("arch", [])),
                codesign_required=bool(t.get("codesign", {}).get("required", False)),
            )
        )
    return targets


def validate(manifest: dict | None = None) -> list[Target]:
    """Raise ManifestError on any problem; return parsed targets on success."""
    m = manifest if manifest is not None else load_manifest()

    if not m.get("version"):
        raise ManifestError("manifest missing version")
    if not isinstance(m.get("first_reply_budget_seconds"), int):
        raise ManifestError("first_reply_budget_seconds must be an int")
    if m["first_reply_budget_seconds"] > 300:
        raise ManifestError("first reply budget exceeds the 5-minute ship bar")

    targets = parse_targets(m)
    seen_os = {t.os for t in targets}

    missing = REQUIRED_OS - seen_os
    if missing:
        raise ManifestError(f"missing target OSes: {sorted(missing)}")

    for t in targets:
        expected = FORMAT_FOR_OS.get(t.os)
        if expected and t.format != expected:
            raise ManifestError(f"{t.os} should ship {expected}, got {t.format}")
        if not t.arch:
            raise ManifestError(f"{t.os} target has no arch list")
        if t.os in CODESIGN_REQUIRED_OS and not t.codesign_required:
            raise ManifestError(f"{t.os} target must require codesigning")

    return targets


def validate_for_os(os_name: str) -> Target:
    """Validate the whole manifest and return the target for a single OS."""
    targets = validate()
    for t in targets:
        if t.os == os_name:
            return t
    raise ManifestError(f"no target for OS {os_name!r}")
