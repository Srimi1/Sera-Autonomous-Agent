"""Signed .skillpack export/import.

A .skillpack is a zip file containing:
  SKILL.md       — the skill manifest
  manifest.json  — {"SKILL.md": "<sha256-hex>", ...}
  SIGNATURE.b64  — (optional) base64(Ed25519 sig over manifest.json bytes)

Outclass: rivals ship unsigned .md. Sera signs every exported pack.
Any post-export tamper — manifest, content, or signature — is rejected on import.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import zipfile
from pathlib import Path

try:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
    )
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PrivateFormat,
        PublicFormat,
        load_pem_private_key,
        load_pem_public_key,
    )
    from cryptography.exceptions import InvalidSignature
    _CRYPTO = True
except ImportError:
    _CRYPTO = False

_SIG_FILE = "SIGNATURE.b64"
_MANIFEST = "manifest.json"

# Strict skill-name regex: lowercase letters / digits / underscore / dash,
# 1-128 chars, no leading dot or dash. Blocks path-traversal payloads from
# malicious YAML frontmatter (e.g. "../../etc/cron.d/x").
_SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,127}$")


class PackError(RuntimeError):
    """Raised on malformed, tampered, or unverifiable skillpacks."""


# ─── Key generation ────────────────────────────────────────────────


def generate_keypair() -> tuple[bytes, bytes]:
    """Return (private_key_pem, public_key_pem) as bytes.

    Requires the `cryptography` package. Raises ImportError if absent.
    """
    if not _CRYPTO:
        raise ImportError("pip install cryptography to use signed skillpacks")
    priv = Ed25519PrivateKey.generate()
    priv_pem = priv.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    pub_pem = priv.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
    return priv_pem, pub_pem


# ─── Pack ──────────────────────────────────────────────────────────


def pack_skill(
    skills_dir: Path,
    skill_name: str,
    out_path: Path,
    *,
    private_key_pem: bytes | None = None,
) -> None:
    """Create a .skillpack at `out_path` from `<skills_dir>/<skill_name>/SKILL.md`.

    If `private_key_pem` is given the pack is also signed (SIGNATURE.b64 added).
    """
    skills_dir = Path(skills_dir)
    skill_md = skills_dir / skill_name / "SKILL.md"
    if not skill_md.exists():
        raise PackError(f"SKILL.md not found: {skill_md}")

    content = skill_md.read_bytes()
    sha = hashlib.sha256(content).hexdigest()
    manifest = {_manifest_key("SKILL.md"): sha}
    manifest_bytes = json.dumps(manifest, sort_keys=True).encode()

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("SKILL.md", content)
        zf.writestr(_MANIFEST, manifest_bytes)
        if private_key_pem is not None:
            sig = _sign_bytes(manifest_bytes, private_key_pem)
            zf.writestr(_SIG_FILE, base64.b64encode(sig))

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(buf.getvalue())


def _manifest_key(name: str) -> str:
    return name


# ─── Sign ──────────────────────────────────────────────────────────


def sign_pack(pack_path: Path, private_key_pem: bytes) -> None:
    """Add/replace SIGNATURE.b64 in an existing pack (signs manifest.json)."""
    pack_path = Path(pack_path)
    with zipfile.ZipFile(pack_path) as zf:
        if _MANIFEST not in zf.namelist():
            raise PackError("pack missing manifest.json")
        manifest_bytes = zf.read(_MANIFEST)
        all_items = [(item, zf.read(item.filename)) for item in zf.infolist()
                     if item.filename != _SIG_FILE]

    sig = _sign_bytes(manifest_bytes, private_key_pem)
    sig_b64 = base64.b64encode(sig)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zout:
        for item, data in all_items:
            zout.writestr(item, data)
        zout.writestr(_SIG_FILE, sig_b64)
    pack_path.write_bytes(buf.getvalue())


# ─── Verify ────────────────────────────────────────────────────────


def verify_pack(pack_path: Path, public_key_pem: bytes) -> None:
    """Verify Ed25519 signature over manifest.json.

    Raises PackError if unsigned, signature invalid, or crypto unavailable.
    """
    if not _CRYPTO:
        raise PackError("pip install cryptography to verify signatures")
    pack_path = Path(pack_path)
    with zipfile.ZipFile(pack_path) as zf:
        names = zf.namelist()
        if _SIG_FILE not in names:
            raise PackError("no signature found in pack (unsigned)")
        if _MANIFEST not in names:
            raise PackError("pack missing manifest.json")
        manifest_bytes = zf.read(_MANIFEST)
        sig = base64.b64decode(zf.read(_SIG_FILE))

    pub = load_pem_public_key(public_key_pem)
    try:
        pub.verify(sig, manifest_bytes)
    except InvalidSignature as e:
        raise PackError("signature verification failed") from e


# ─── Unpack ────────────────────────────────────────────────────────


def unpack_skill(
    pack_path: Path,
    skills_dir: Path,
    *,
    public_key_pem: bytes | None = None,
) -> str:
    """Extract a .skillpack to `<skills_dir>/<name>/`.

    If `public_key_pem` is given, verifies the signature before extracting.
    Raises PackError on hash mismatch or bad signature.
    Returns the skill name (derived from the restored SKILL.md).
    """
    pack_path = Path(pack_path)
    skills_dir = Path(skills_dir)

    if public_key_pem is not None:
        verify_pack(pack_path, public_key_pem)

    with zipfile.ZipFile(pack_path) as zf:
        if _MANIFEST not in zf.namelist():
            raise PackError("pack missing manifest.json")
        manifest = json.loads(zf.read(_MANIFEST))

        # Verify hashes before writing anything.
        for filename, expected_sha in manifest.items():
            if filename not in zf.namelist():
                raise PackError(f"manifest references missing file: {filename}")
            actual = hashlib.sha256(zf.read(filename)).hexdigest()
            if actual != expected_sha:
                raise PackError(
                    f"hash mismatch for {filename}: expected {expected_sha[:8]}… got {actual[:8]}…"
                )

        skill_content = zf.read("SKILL.md")

    # Parse skill name from YAML frontmatter and enforce the strict regex.
    # The YAML is untrusted input — without sanitisation a malicious pack
    # could write SKILL.md anywhere on disk by setting `name: ../../...`.
    skill_name = _parse_skill_name(skill_content)
    if not _SKILL_NAME_RE.match(skill_name):
        raise PackError(
            f"invalid skill name {skill_name!r}: must match {_SKILL_NAME_RE.pattern}"
        )

    skills_dir_resolved = skills_dir.resolve()
    dest = (skills_dir / skill_name).resolve()
    # Defence-in-depth: even if the regex were ever relaxed, refuse any
    # destination that escapes the skills directory.
    if skills_dir_resolved != dest.parent:
        raise PackError(
            f"skill destination escapes skills_dir: {dest} not under {skills_dir_resolved}"
        )

    dest.mkdir(parents=True, exist_ok=True)
    (dest / "SKILL.md").write_bytes(skill_content)
    return skill_name


def _parse_skill_name(content: bytes) -> str:
    """Extract `name:` from YAML frontmatter; fall back to 'unknown'."""
    text = content.decode(errors="replace")
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("name:"):
            return stripped[5:].strip()
    return "unknown"


# ─── Internal crypto ───────────────────────────────────────────────


def _sign_bytes(data: bytes, private_key_pem: bytes) -> bytes:
    if not _CRYPTO:
        raise PackError("pip install cryptography to sign packs")
    priv = load_pem_private_key(private_key_pem, password=None)
    return priv.sign(data)
