"""Marketplace client — publish, install, search (P-96).

Handles verification before extraction: every `install` call checks the
Ed25519 signature (if a pubkey is registered) before touching the filesystem.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sera.marketplace.registry import MarketplaceRegistry, PackEntry


@dataclass
class InstallResult:
    name: str
    kind: str
    pack_id: str
    dest: Path
    verified: bool    # True if signature was checked and passed


@dataclass
class PublishResult:
    entry: PackEntry
    signed: bool


class MarketplaceClient:
    """High-level install/publish/search operations over a registry."""

    def __init__(
        self,
        registry: MarketplaceRegistry | None = None,
        skills_dir: Path | None = None,
        redteam_dir: Path | None = None,
    ) -> None:
        self._registry = registry or MarketplaceRegistry()
        self._skills_dir = skills_dir or (Path.home() / ".sera" / "skills")
        self._redteam_dir = redteam_dir or (Path.home() / ".sera" / "redteam")

    def publish(
        self,
        pack_path: str | Path,
        *,
        name: str | None = None,
        description: str = "",
        tags: list[str] | None = None,
        pubkey_pem: bytes | None = None,
    ) -> PublishResult:
        """Register a .skillpack or .redpack in the local marketplace."""
        p = Path(pack_path).resolve()
        if not p.exists():
            raise FileNotFoundError(p)

        kind = _infer_kind(p)
        pack_name = name or p.stem

        entry = self._registry.publish(
            name=pack_name,
            kind=kind,
            path=str(p),
            pubkey_pem=pubkey_pem.decode("utf-8") if pubkey_pem else None,
            description=description,
            tags=tags,
        )
        return PublishResult(entry=entry, signed=pubkey_pem is not None)

    def install(
        self,
        name: str,
        *,
        kind: str | None = None,
        pubkey_pem: bytes | None = None,
        dest_dir: Path | None = None,
    ) -> InstallResult:
        """Install a registered pack by name, verifying signature if pubkey given."""
        entry = self._registry.get_by_name(name, kind=kind)
        if entry is None:
            raise KeyError(f"no pack named {name!r} in the registry")

        pack_path = Path(entry.path)
        if not pack_path.exists():
            raise FileNotFoundError(f"registered path not found: {pack_path}")

        # Resolve pubkey: caller-supplied > registry-stored > none
        key_pem = pubkey_pem
        if key_pem is None and entry.pubkey_pem:
            key_pem = entry.pubkey_pem.encode("utf-8")

        verified = False
        if entry.kind == "skillpack":
            dest = self._install_skillpack(pack_path, key_pem, dest_dir)
            verified = key_pem is not None
        elif entry.kind == "redpack":
            dest = self._install_redpack(pack_path, key_pem, dest_dir)
            verified = key_pem is not None
        else:
            raise ValueError(f"unknown kind: {entry.kind!r}")

        self._registry.mark_installed(entry.id)
        return InstallResult(
            name=entry.name,
            kind=entry.kind,
            pack_id=entry.id,
            dest=dest,
            verified=verified,
        )

    def search(self, query: str = "", kind: str | None = None) -> list[PackEntry]:
        if not query:
            return self._registry.list_all(kind=kind)
        return self._registry.search(query, kind=kind)

    def list_installed(self) -> list[PackEntry]:
        return self._registry.list_installed()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _install_skillpack(
        self, pack_path: Path, pubkey_pem: bytes | None, dest_dir: Path | None
    ) -> Path:
        from sera.skills.pack import unpack_skill
        target = dest_dir or self._skills_dir
        target.mkdir(parents=True, exist_ok=True)
        skill_name = unpack_skill(pack_path, target, public_key_pem=pubkey_pem)
        return target / skill_name

    def _install_redpack(
        self, pack_path: Path, pubkey_pem: bytes | None, dest_dir: Path | None
    ) -> Path:
        from sera.redteam.pack import load_redpack
        # Verify (will raise RedPackError on tamper/bad sig)
        load_redpack(pack_path, public_key_pem=pubkey_pem)
        # Copy into redteam dir
        target = dest_dir or self._redteam_dir
        target.mkdir(parents=True, exist_ok=True)
        dest = target / pack_path.name
        shutil.copy2(pack_path, dest)
        return dest


def _infer_kind(path: Path) -> str:
    suffix = path.suffix.lower()
    stem = path.stem.lower()
    if suffix == ".skillpack" or "skill" in stem:
        return "skillpack"
    if suffix == ".redpack" or "red" in stem:
        return "redpack"
    raise ValueError(
        f"cannot infer kind from {path.name!r}; "
        "pass kind= explicitly or use .skillpack / .redpack extension"
    )
