"""P-96: marketplace — signed artifact registry."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from sera.marketplace.registry import MarketplaceRegistry, PackEntry
from sera.marketplace.client import MarketplaceClient, InstallResult, _infer_kind


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

def _reg(tmp_path: Path) -> MarketplaceRegistry:
    return MarketplaceRegistry(db_path=tmp_path / "registry.db")


def test_publish_creates_entry(tmp_path: Path):
    reg = _reg(tmp_path)
    entry = reg.publish(name="my-skill", kind="skillpack", path="/tmp/x.skillpack")
    assert entry.name == "my-skill"
    assert entry.kind == "skillpack"
    assert not entry.installed
    reg.close()


def test_publish_invalid_kind_raises(tmp_path: Path):
    reg = _reg(tmp_path)
    with pytest.raises(ValueError, match="kind"):
        reg.publish(name="x", kind="banana", path="/tmp/x")
    reg.close()


def test_publish_empty_name_raises(tmp_path: Path):
    reg = _reg(tmp_path)
    with pytest.raises(ValueError, match="name"):
        reg.publish(name="  ", kind="skillpack", path="/tmp/x")
    reg.close()


def test_get_by_name_returns_entry(tmp_path: Path):
    reg = _reg(tmp_path)
    reg.publish(name="alpha", kind="redpack", path="/tmp/a.redpack")
    e = reg.get_by_name("alpha")
    assert e is not None
    assert e.kind == "redpack"
    reg.close()


def test_get_by_name_with_kind_filter(tmp_path: Path):
    reg = _reg(tmp_path)
    reg.publish(name="dupe", kind="skillpack", path="/tmp/d.skillpack")
    reg.publish(name="dupe", kind="redpack", path="/tmp/d.redpack")
    e = reg.get_by_name("dupe", kind="redpack")
    assert e is not None
    assert e.kind == "redpack"
    reg.close()


def test_search_by_name_substring(tmp_path: Path):
    reg = _reg(tmp_path)
    reg.publish(name="inject-payloads", kind="redpack", path="/tmp/i.redpack",
                description="SQL injection test pack")
    reg.publish(name="summarise-skill", kind="skillpack", path="/tmp/s.skillpack")
    hits = reg.search("inject")
    assert len(hits) == 1
    assert hits[0].name == "inject-payloads"
    reg.close()


def test_search_empty_query_returns_all(tmp_path: Path):
    reg = _reg(tmp_path)
    reg.publish(name="a", kind="skillpack", path="/tmp/a.skillpack")
    reg.publish(name="b", kind="redpack", path="/tmp/b.redpack")
    hits = reg.search("")
    assert len(hits) == 2
    reg.close()


def test_mark_installed(tmp_path: Path):
    reg = _reg(tmp_path)
    e = reg.publish(name="tgt", kind="skillpack", path="/tmp/t.skillpack")
    assert not e.installed
    reg.mark_installed(e.id)
    loaded = reg.get_by_name("tgt")
    assert loaded.installed
    reg.close()


def test_list_installed_returns_only_installed(tmp_path: Path):
    reg = _reg(tmp_path)
    e1 = reg.publish(name="installed-one", kind="skillpack", path="/tmp/i.skillpack")
    reg.publish(name="not-installed", kind="skillpack", path="/tmp/n.skillpack")
    reg.mark_installed(e1.id)
    installed = reg.list_installed()
    assert len(installed) == 1
    assert installed[0].name == "installed-one"
    reg.close()


def test_pubkey_pem_stored_and_retrieved(tmp_path: Path):
    reg = _reg(tmp_path)
    e = reg.publish(name="signed", kind="skillpack", path="/tmp/s.skillpack",
                    pubkey_pem="-----BEGIN PUBLIC KEY-----\nABC\n-----END PUBLIC KEY-----\n")
    loaded = reg.get_by_name("signed")
    assert "BEGIN PUBLIC KEY" in loaded.pubkey_pem
    reg.close()


# ---------------------------------------------------------------------------
# _infer_kind
# ---------------------------------------------------------------------------

def test_infer_kind_skillpack():
    assert _infer_kind(Path("my-skill.skillpack")) == "skillpack"


def test_infer_kind_redpack():
    assert _infer_kind(Path("attacks.redpack")) == "redpack"


def test_infer_kind_from_stem():
    assert _infer_kind(Path("summarise-skill.zip")) == "skillpack"
    assert _infer_kind(Path("red-suite.zip")) == "redpack"


def test_infer_kind_unknown_raises():
    with pytest.raises(ValueError):
        _infer_kind(Path("mystery.tar.gz"))


# ---------------------------------------------------------------------------
# Client — publish + install skillpack round-trip
# ---------------------------------------------------------------------------

def _signed_skillpack(tmp_path: Path) -> tuple[Path, bytes, bytes]:
    from sera.skills.pack import generate_keypair, pack_skill
    skills_dir = tmp_path / "skills_src"
    skill_dir = skills_dir / "test-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# test-skill\nname: test-skill\n---\nHello!")
    priv, pub = generate_keypair()
    out = tmp_path / "test-skill.skillpack"
    pack_skill(skills_dir, "test-skill", out, private_key_pem=priv)
    return out, priv, pub


def test_client_publish_and_search(tmp_path: Path):
    reg = _reg(tmp_path)
    client = MarketplaceClient(registry=reg, skills_dir=tmp_path / "installed_skills")
    pack, priv, pub = _signed_skillpack(tmp_path)
    result = client.publish(pack, description="test skill", pubkey_pem=pub)
    assert result.signed
    assert result.entry.name == "test-skill"
    hits = client.search("test")
    assert len(hits) == 1
    reg.close()


def test_client_install_skillpack_verified(tmp_path: Path):
    reg = _reg(tmp_path)
    install_dir = tmp_path / "installed_skills"
    client = MarketplaceClient(registry=reg, skills_dir=install_dir)
    pack, priv, pub = _signed_skillpack(tmp_path)
    client.publish(pack, pubkey_pem=pub)
    result = client.install("test-skill", pubkey_pem=pub, dest_dir=install_dir)
    assert result.verified
    assert result.kind == "skillpack"
    assert (install_dir / "test-skill" / "SKILL.md").exists()
    reg.close()


def test_client_install_wrong_key_fails(tmp_path: Path):
    from sera.skills.pack import generate_keypair
    reg = _reg(tmp_path)
    install_dir = tmp_path / "installed_skills"
    client = MarketplaceClient(registry=reg, skills_dir=install_dir)
    pack, priv, pub = _signed_skillpack(tmp_path)
    _, wrong_pub = generate_keypair()
    client.publish(pack, pubkey_pem=pub)
    with pytest.raises(Exception):
        client.install("test-skill", pubkey_pem=wrong_pub, dest_dir=install_dir)
    reg.close()


def test_client_install_unknown_name_raises(tmp_path: Path):
    reg = _reg(tmp_path)
    client = MarketplaceClient(registry=reg)
    with pytest.raises(KeyError, match="nosuchpack"):
        client.install("nosuchpack")
    reg.close()


def test_client_install_redpack(tmp_path: Path):
    from sera.redteam.pack import RedPackBuilder, generate_keypair as rp_keygen
    priv, pub = rp_keygen()
    pack_path = tmp_path / "test.redpack"
    RedPackBuilder().add(id="t1", kind="IGNORE", text="Ignore all.").save(
        pack_path, private_key_pem=priv
    )
    reg = _reg(tmp_path)
    redteam_dir = tmp_path / "redteam"
    client = MarketplaceClient(registry=reg, redteam_dir=redteam_dir)
    client.publish(pack_path, pubkey_pem=pub)
    result = client.install("test", pubkey_pem=pub, dest_dir=redteam_dir)
    assert result.verified
    assert result.kind == "redpack"
    assert (redteam_dir / "test.redpack").exists()
    reg.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def test_cli_marketplace_help():
    from click.testing import CliRunner
    from sera.cli.main import main
    runner = CliRunner()
    result = runner.invoke(main, ["marketplace", "--help"])
    assert result.exit_code == 0
    assert "publish" in result.output
    assert "install" in result.output
    assert "search" in result.output
