"""P-28: signed .skillpack export/import (TDD)."""
from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from sera.skills.pack import (
    PackError,
    generate_keypair,
    pack_skill,
    sign_pack,
    unpack_skill,
    verify_pack,
)


# ─── Helpers ──────────────────────────────────────────────────────


def _write_skill(skills_dir: Path, name: str, body: str = "# skill body") -> Path:
    d = skills_dir / name
    d.mkdir(parents=True, exist_ok=True)
    p = d / "SKILL.md"
    p.write_text(
        "---\n"
        f"name: {name}\n"
        "trigger: /x\n"
        "permission: READ_ONLY\n"
        "version: 0.1.0\n"
        "---\n"
        f"{body}\n"
    )
    return p


# ─── Cycle 1: pack/unpack round-trip ──────────────────────────────


def test_pack_creates_zip_with_skill_and_manifest(tmp_path: Path):
    _write_skill(tmp_path / "skills", "alpha")
    out = tmp_path / "alpha.skillpack"
    pack_skill(tmp_path / "skills", "alpha", out)
    assert out.exists()
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
    assert "SKILL.md" in names
    assert "manifest.json" in names


def test_pack_manifest_contains_sha256(tmp_path: Path):
    import json, hashlib
    _write_skill(tmp_path / "skills", "beta", body="some content")
    out = tmp_path / "beta.skillpack"
    pack_skill(tmp_path / "skills", "beta", out)
    with zipfile.ZipFile(out) as zf:
        manifest = json.loads(zf.read("manifest.json"))
        skill_bytes = zf.read("SKILL.md")
    expected = hashlib.sha256(skill_bytes).hexdigest()
    assert manifest["SKILL.md"] == expected


def test_unpack_restores_skill(tmp_path: Path):
    src_dir = tmp_path / "src"
    _write_skill(src_dir, "gamma", body="hello world")
    out = tmp_path / "gamma.skillpack"
    pack_skill(src_dir, "gamma", out)

    dst_dir = tmp_path / "dst"
    unpack_skill(out, dst_dir)
    restored = dst_dir / "gamma" / "SKILL.md"
    assert restored.exists()
    assert "hello world" in restored.read_text()


def test_unpack_rejects_tampered_content(tmp_path: Path):
    _write_skill(tmp_path / "skills", "delta")
    out = tmp_path / "delta.skillpack"
    pack_skill(tmp_path / "skills", "delta", out)

    # Corrupt SKILL.md inside the zip.
    import io
    buf = io.BytesIO()
    with zipfile.ZipFile(out) as zin, zipfile.ZipFile(buf, "w") as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "SKILL.md":
                data = b"TAMPERED"
            zout.writestr(item, data)
    buf.seek(0)
    out.write_bytes(buf.read())

    with pytest.raises(PackError, match="hash mismatch"):
        unpack_skill(out, tmp_path / "dst")


# ─── Cycle 2: key generation + sign/verify ────────────────────────


def test_generate_keypair_returns_pem_bytes():
    priv, pub = generate_keypair()
    assert priv.startswith(b"-----BEGIN PRIVATE KEY-----")
    assert pub.startswith(b"-----BEGIN PUBLIC KEY-----")


def test_sign_and_verify_pack(tmp_path: Path):
    _write_skill(tmp_path / "skills", "epsilon")
    out = tmp_path / "epsilon.skillpack"
    pack_skill(tmp_path / "skills", "epsilon", out)

    priv, pub = generate_keypair()
    sign_pack(out, priv)

    # Should not raise.
    verify_pack(out, pub)


def test_verify_rejects_wrong_key(tmp_path: Path):
    _write_skill(tmp_path / "skills", "zeta")
    out = tmp_path / "zeta.skillpack"
    pack_skill(tmp_path / "skills", "zeta", out)

    priv, _ = generate_keypair()
    sign_pack(out, priv)

    _, wrong_pub = generate_keypair()
    with pytest.raises(PackError, match="signature"):
        verify_pack(out, wrong_pub)


def test_verify_rejects_unsigned_pack(tmp_path: Path):
    _write_skill(tmp_path / "skills", "eta")
    out = tmp_path / "eta.skillpack"
    pack_skill(tmp_path / "skills", "eta", out)

    _, pub = generate_keypair()
    with pytest.raises(PackError, match="no signature"):
        verify_pack(out, pub)


# ─── Cycle 3: tamper detection after signing ──────────────────────


def test_verify_fails_on_post_sign_tamper(tmp_path: Path):
    _write_skill(tmp_path / "skills", "theta", body="original")
    out = tmp_path / "theta.skillpack"
    pack_skill(tmp_path / "skills", "theta", out)

    priv, pub = generate_keypair()
    sign_pack(out, priv)

    # Tamper manifest.json after signing.
    import io, json
    buf = io.BytesIO()
    with zipfile.ZipFile(out) as zin, zipfile.ZipFile(buf, "w") as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "manifest.json":
                m = json.loads(data)
                m["SKILL.md"] = "0" * 64  # forged hash
                data = json.dumps(m).encode()
            zout.writestr(item, data)
    buf.seek(0)
    out.write_bytes(buf.read())

    with pytest.raises(PackError, match="signature"):
        verify_pack(out, pub)


def test_unpack_with_key_verifies_sig(tmp_path: Path):
    _write_skill(tmp_path / "skills", "iota")
    out = tmp_path / "iota.skillpack"
    pack_skill(tmp_path / "skills", "iota", out)

    priv, pub = generate_keypair()
    sign_pack(out, priv)

    dst = tmp_path / "dst"
    unpack_skill(out, dst, public_key_pem=pub)
    assert (dst / "iota" / "SKILL.md").exists()


def test_unpack_with_key_rejects_tamper(tmp_path: Path):
    _write_skill(tmp_path / "skills", "kappa", body="real")
    out = tmp_path / "kappa.skillpack"
    pack_skill(tmp_path / "skills", "kappa", out)

    priv, pub = generate_keypair()
    sign_pack(out, priv)

    # Tamper after signing.
    import io
    buf = io.BytesIO()
    with zipfile.ZipFile(out) as zin, zipfile.ZipFile(buf, "w") as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "SKILL.md":
                data = b"TAMPERED"
            zout.writestr(item, data)
    buf.seek(0)
    out.write_bytes(buf.read())

    with pytest.raises(PackError):
        unpack_skill(out, tmp_path / "dst", public_key_pem=pub)


# ─── Cycle 4: CLI export / import ─────────────────────────────────


def test_cli_skills_export_creates_pack(tmp_path: Path):
    from click.testing import CliRunner
    from sera.cli.main import main

    _write_skill(tmp_path / "skills", "lambda_skill")
    out = tmp_path / "out.skillpack"
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["skills", "--root", str(tmp_path / "skills"),
         "export", "lambda_skill", "--out", str(out)],
    )
    assert result.exit_code == 0, result.output
    assert out.exists()


def test_cli_skills_import_restores_skill(tmp_path: Path):
    from click.testing import CliRunner
    from sera.cli.main import main

    _write_skill(tmp_path / "src_skills", "mu_skill", body="mu body")
    out = tmp_path / "mu.skillpack"
    runner = CliRunner()

    # Export
    runner.invoke(
        main,
        ["skills", "--root", str(tmp_path / "src_skills"),
         "export", "mu_skill", "--out", str(out)],
    )

    # Import to fresh dir
    dst = tmp_path / "dst_skills"
    result = runner.invoke(
        main,
        ["skills", "--root", str(dst), "import", str(out)],
    )
    assert result.exit_code == 0, result.output
    assert (dst / "mu_skill" / "SKILL.md").exists()


def test_cli_skills_export_with_key(tmp_path: Path):
    from click.testing import CliRunner
    from sera.cli.main import main

    _write_skill(tmp_path / "skills", "nu_skill")
    priv, pub = generate_keypair()
    key_file = tmp_path / "key.pem"
    key_file.write_bytes(priv)
    out = tmp_path / "nu.skillpack"

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["skills", "--root", str(tmp_path / "skills"),
         "export", "nu_skill", "--out", str(out), "--key", str(key_file)],
    )
    assert result.exit_code == 0, result.output

    # Verify sig present.
    with zipfile.ZipFile(out) as zf:
        assert "SIGNATURE.b64" in zf.namelist()


def test_cli_skills_import_rejects_bad_sig(tmp_path: Path):
    from click.testing import CliRunner
    from sera.cli.main import main

    _write_skill(tmp_path / "skills", "xi_skill")
    priv, pub = generate_keypair()
    _, wrong_pub = generate_keypair()
    key_file = tmp_path / "priv.pem"
    key_file.write_bytes(priv)
    pub_file = tmp_path / "wrong_pub.pem"
    pub_file.write_bytes(wrong_pub)
    out = tmp_path / "xi.skillpack"

    runner = CliRunner()
    runner.invoke(
        main,
        ["skills", "--root", str(tmp_path / "skills"),
         "export", "xi_skill", "--out", str(out), "--key", str(key_file)],
    )

    dst = tmp_path / "dst"
    result = runner.invoke(
        main,
        ["skills", "--root", str(dst),
         "import", str(out), "--key", str(pub_file)],
    )
    assert result.exit_code != 0
    assert "signature" in result.output.lower()


# ─── P-AUDIT-1: path-traversal regression tests (audit P1 fix) ──────────────


def _build_malicious_pack(tmp_path: Path, evil_name: str) -> Path:
    """Build a .skillpack whose SKILL.md frontmatter declares a traversal name."""
    import hashlib
    import json
    skill_md = (
        "---\n"
        f"name: {evil_name}\n"
        "trigger: /x\n"
        "permission: READ_ONLY\n"
        "version: 0.1.0\n"
        "---\n"
        "evil body\n"
    ).encode()

    pack = tmp_path / "evil.skillpack"
    with zipfile.ZipFile(pack, "w") as zf:
        zf.writestr("SKILL.md", skill_md)
        manifest = {"SKILL.md": hashlib.sha256(skill_md).hexdigest()}
        zf.writestr("manifest.json", json.dumps(manifest))
    return pack


def test_unpack_rejects_traversal_dotdot(tmp_path: Path):
    pack = _build_malicious_pack(tmp_path, "../../etc/cron.d/x")
    with pytest.raises(PackError, match="invalid skill name"):
        unpack_skill(pack, tmp_path / "skills")
    assert not (tmp_path / "etc" / "cron.d").exists()


def test_unpack_rejects_absolute_path(tmp_path: Path):
    pack = _build_malicious_pack(tmp_path, "/tmp/evil_skill")
    with pytest.raises(PackError, match="invalid skill name"):
        unpack_skill(pack, tmp_path / "skills")


def test_unpack_rejects_slash_in_name(tmp_path: Path):
    pack = _build_malicious_pack(tmp_path, "foo/bar")
    with pytest.raises(PackError, match="invalid skill name"):
        unpack_skill(pack, tmp_path / "skills")


def test_unpack_rejects_uppercase(tmp_path: Path):
    pack = _build_malicious_pack(tmp_path, "MyEvilSkill")
    with pytest.raises(PackError, match="invalid skill name"):
        unpack_skill(pack, tmp_path / "skills")


def test_unpack_rejects_empty_name(tmp_path: Path):
    pack = _build_malicious_pack(tmp_path, "")
    with pytest.raises(PackError, match="invalid skill name"):
        unpack_skill(pack, tmp_path / "skills")


def test_unpack_rejects_leading_dash(tmp_path: Path):
    pack = _build_malicious_pack(tmp_path, "-rf")
    with pytest.raises(PackError, match="invalid skill name"):
        unpack_skill(pack, tmp_path / "skills")


def test_unpack_accepts_clean_name(tmp_path: Path):
    pack = _build_malicious_pack(tmp_path, "my_skill_v2")
    name = unpack_skill(pack, tmp_path / "skills")
    assert name == "my_skill_v2"
    assert (tmp_path / "skills" / "my_skill_v2" / "SKILL.md").exists()
