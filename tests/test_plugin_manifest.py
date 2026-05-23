"""Tests for sera.plugins.manifest — plugin manifest spec + loader + signatures."""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest
import yaml

from sera.plugins.manifest import (
    Capability,
    LoadResult,
    ManifestError,
    PluginManifest,
    SignatureBlock,
    ToolEntry,
    canonical_payload,
    load_manifest_dict,
    load_manifest_file,
    load_plugin,
    manifest_digest,
    parse_capability,
    sign_manifest,
    unload_plugin,
    validate_capabilities,
    verify_signature,
)
from sera.tools.base import Permission, ToolContext
from sera.tools.registry import all_tools, reset as reset_registry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _minimal_dict() -> dict:
    return {
        "name": "weather-tools",
        "version": "1.0.0",
        "entrypoint": "_test_plugin",
        "tools": [
            {
                "name": "get_weather",
                "attr": "get_weather",
                "permission": "READ_ONLY",
                "description": "Get weather for a city.",
                "parameters": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                },
            }
        ],
        "capabilities": ["net.fetch", "tools.register"],
    }


def _write_plugin_module(plugin_dir: Path) -> None:
    """Write a hand-written test plugin module to disk."""
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "_test_plugin.py").write_text(
        "def get_weather(args, ctx):\n"
        "    city = args.get('city', '?')\n"
        "    return f'Weather in {city}: sunny, 72F'\n"
    )


# ---------------------------------------------------------------------------
# Capability vocabulary
# ---------------------------------------------------------------------------

class TestCapabilities:
    def test_parse_bare(self) -> None:
        assert parse_capability("net.fetch") == ("net.fetch", "")

    def test_parse_scoped(self) -> None:
        assert parse_capability("fs.read:/tmp") == ("fs.read", "/tmp")

    def test_validate_all_known(self) -> None:
        assert validate_capabilities(["net.fetch", "fs.read:/tmp", "tools.register"]) == []

    def test_validate_unknown(self) -> None:
        bad = validate_capabilities(["foo.bar"])
        assert "foo.bar" in bad

    def test_capability_enum(self) -> None:
        assert Capability.NET_FETCH.value == "net.fetch"
        assert Capability.TOOLS_REGISTER.value == "tools.register"


# ---------------------------------------------------------------------------
# Manifest parsing
# ---------------------------------------------------------------------------

class TestManifestParsing:
    def test_minimal_valid(self) -> None:
        m = load_manifest_dict(_minimal_dict())
        assert m.name == "weather-tools"
        assert m.version == "1.0.0"
        assert m.entrypoint == "_test_plugin"
        assert len(m.tools) == 1
        assert m.tools[0].name == "get_weather"

    def test_missing_required_field(self) -> None:
        d = _minimal_dict()
        del d["name"]
        with pytest.raises(ManifestError, match="missing required"):
            load_manifest_dict(d)

    def test_invalid_permission(self) -> None:
        d = _minimal_dict()
        d["tools"][0]["permission"] = "GOD_MODE"
        with pytest.raises(ManifestError, match="invalid permission"):
            load_manifest_dict(d)

    def test_unknown_capability(self) -> None:
        d = _minimal_dict()
        d["capabilities"] = ["net.fetch", "evil.do"]
        with pytest.raises(ManifestError, match="unknown capabilities"):
            load_manifest_dict(d)

    def test_empty_tools_ok(self) -> None:
        d = _minimal_dict()
        d["tools"] = []
        m = load_manifest_dict(d)
        assert m.tools == ()

    def test_load_from_file(self, tmp_path: Path) -> None:
        path = tmp_path / "plugin.yaml"
        path.write_text(yaml.safe_dump(_minimal_dict()))
        m = load_manifest_file(path)
        assert m.name == "weather-tools"

    def test_load_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(ManifestError, match="not found"):
            load_manifest_file(tmp_path / "nope.yaml")

    def test_load_non_mapping_root(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.yaml"
        path.write_text("- just a list")
        with pytest.raises(ManifestError, match="mapping"):
            load_manifest_file(path)


# ---------------------------------------------------------------------------
# Canonical payload + signing
# ---------------------------------------------------------------------------

class TestCanonicalPayload:
    def test_deterministic(self) -> None:
        m = load_manifest_dict(_minimal_dict())
        assert canonical_payload(m) == canonical_payload(m)

    def test_excludes_signature(self) -> None:
        m1 = load_manifest_dict(_minimal_dict())
        # Build a manifest with a signature; canonical payload must equal m1's
        m2 = PluginManifest(
            name=m1.name, version=m1.version, entrypoint=m1.entrypoint,
            author=m1.author, description=m1.description,
            capabilities=m1.capabilities, tools=m1.tools,
            signature=SignatureBlock("ed25519", "AA==", "BB=="),
        )
        assert canonical_payload(m1) == canonical_payload(m2)

    def test_digest_is_hex(self) -> None:
        m = load_manifest_dict(_minimal_dict())
        d = manifest_digest(m)
        assert len(d) == 64
        assert all(c in "0123456789abcdef" for c in d)


class TestSignatures:
    def _keypair(self) -> bytes:
        return os.urandom(32)

    def test_sign_then_verify(self) -> None:
        m = load_manifest_dict(_minimal_dict())
        priv = self._keypair()
        sig = sign_manifest(m, priv)
        signed = PluginManifest(
            name=m.name, version=m.version, entrypoint=m.entrypoint,
            author=m.author, description=m.description,
            capabilities=m.capabilities, tools=m.tools, signature=sig,
        )
        assert verify_signature(signed) is True

    def test_tampered_manifest_fails(self) -> None:
        m = load_manifest_dict(_minimal_dict())
        priv = self._keypair()
        sig = sign_manifest(m, priv)
        tampered = PluginManifest(
            name="evil-plugin",        # changed name
            version=m.version, entrypoint=m.entrypoint,
            author=m.author, description=m.description,
            capabilities=m.capabilities, tools=m.tools, signature=sig,
        )
        assert verify_signature(tampered) is False

    def test_unsigned_returns_false(self) -> None:
        m = load_manifest_dict(_minimal_dict())
        assert verify_signature(m) is False

    def test_unsupported_algorithm(self) -> None:
        m = load_manifest_dict(_minimal_dict())
        bad = PluginManifest(
            name=m.name, version=m.version, entrypoint=m.entrypoint,
            signature=SignatureBlock("rsa-2048", "AA==", "BB=="),
        )
        with pytest.raises(ManifestError, match="unsupported"):
            verify_signature(bad)


# ---------------------------------------------------------------------------
# Plugin loading — P-47 verification
# ---------------------------------------------------------------------------

class TestPluginLoading:
    def setup_method(self) -> None:
        reset_registry()
        # Drop any cached test plugin module
        sys.modules.pop("_test_plugin", None)

    def teardown_method(self) -> None:
        reset_registry()
        sys.modules.pop("_test_plugin", None)

    def test_hand_written_plugin_loads(self, tmp_path: Path) -> None:
        """Verification: hand-written plugin loads + tools register without core changes."""
        _write_plugin_module(tmp_path)
        manifest = load_manifest_dict(_minimal_dict())
        result = load_plugin(manifest, plugin_dir=tmp_path)
        assert result.plugin_name == "weather-tools"
        assert len(result.tool_names) == 1
        # Tool now appears in registry without restart or core changes
        names = {t.name for t in all_tools()}
        assert "plugin_weather-tools__get_weather" in names

    def test_loaded_tool_handler_runs(self, tmp_path: Path) -> None:
        _write_plugin_module(tmp_path)
        manifest = load_manifest_dict(_minimal_dict())
        load_plugin(manifest, plugin_dir=tmp_path)
        tool = next(t for t in all_tools() if t.name.startswith("plugin_weather-tools__"))
        ctx = ToolContext(session_id="t", workspace="/tmp")
        result = asyncio.run(tool.handler({"city": "Tokyo"}, ctx))
        assert "Tokyo" in result
        assert "sunny" in result

    def test_loaded_tool_has_declared_permission(self, tmp_path: Path) -> None:
        _write_plugin_module(tmp_path)
        manifest = load_manifest_dict(_minimal_dict())
        load_plugin(manifest, plugin_dir=tmp_path)
        tool = next(t for t in all_tools() if t.name.startswith("plugin_weather-tools__"))
        assert tool.permission == Permission.READ_ONLY

    def test_missing_attr_raises(self, tmp_path: Path) -> None:
        _write_plugin_module(tmp_path)
        d = _minimal_dict()
        d["tools"][0]["attr"] = "nonexistent_function"
        manifest = load_manifest_dict(d)
        with pytest.raises(ManifestError, match="no attr"):
            load_plugin(manifest, plugin_dir=tmp_path)

    def test_missing_entrypoint_raises(self, tmp_path: Path) -> None:
        # plugin_dir exists but no module by this name anywhere
        d = _minimal_dict()
        d["entrypoint"] = "_definitely_not_a_real_module_xyz123"
        manifest = load_manifest_dict(d)
        with pytest.raises(ManifestError, match="cannot import"):
            load_plugin(manifest, plugin_dir=tmp_path)

    def test_require_signature_refuses_unsigned(self, tmp_path: Path) -> None:
        _write_plugin_module(tmp_path)
        manifest = load_manifest_dict(_minimal_dict())
        with pytest.raises(ManifestError, match="unsigned"):
            load_plugin(manifest, plugin_dir=tmp_path, require_signature=True)

    def test_invalid_signature_refuses(self, tmp_path: Path) -> None:
        _write_plugin_module(tmp_path)
        m = load_manifest_dict(_minimal_dict())
        # Forge a bad signature
        forged = PluginManifest(
            name=m.name, version=m.version, entrypoint=m.entrypoint,
            tools=m.tools, capabilities=m.capabilities,
            signature=SignatureBlock("ed25519", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=", "BB=="),
        )
        with pytest.raises(ManifestError, match="signature"):
            load_plugin(forged, plugin_dir=tmp_path)

    def test_valid_signature_loads(self, tmp_path: Path) -> None:
        _write_plugin_module(tmp_path)
        m = load_manifest_dict(_minimal_dict())
        priv = os.urandom(32)
        sig = sign_manifest(m, priv)
        signed = PluginManifest(
            name=m.name, version=m.version, entrypoint=m.entrypoint,
            tools=m.tools, capabilities=m.capabilities, signature=sig,
        )
        result = load_plugin(signed, plugin_dir=tmp_path, require_signature=True)
        assert result.signed is True

    def test_unload_removes_tools(self, tmp_path: Path) -> None:
        _write_plugin_module(tmp_path)
        manifest = load_manifest_dict(_minimal_dict())
        result = load_plugin(manifest, plugin_dir=tmp_path)
        removed = unload_plugin(result)
        assert removed == 1
        names = {t.name for t in all_tools()}
        assert "plugin_weather-tools__get_weather" not in names

    def test_full_round_trip_via_yaml_file(self, tmp_path: Path) -> None:
        """Write plugin.yaml + module to disk, load, verify tool runs."""
        plugin_dir = tmp_path
        _write_plugin_module(plugin_dir)
        yaml_path = plugin_dir / "plugin.yaml"
        yaml_path.write_text(yaml.safe_dump(_minimal_dict()))
        manifest = load_manifest_file(yaml_path)
        result = load_plugin(manifest, plugin_dir=plugin_dir)
        assert result.tool_names
        tool = next(t for t in all_tools() if t.name in result.tool_names)
        ctx = ToolContext(session_id="t", workspace="/tmp")
        out = asyncio.run(tool.handler({"city": "Lagos"}, ctx))
        assert "Lagos" in out


# ---------------------------------------------------------------------------
# LoadResult shape
# ---------------------------------------------------------------------------

class TestLoadResult:
    def setup_method(self) -> None:
        reset_registry()
        sys.modules.pop("_test_plugin", None)

    def teardown_method(self) -> None:
        reset_registry()
        sys.modules.pop("_test_plugin", None)

    def test_capabilities_passed_through(self, tmp_path: Path) -> None:
        _write_plugin_module(tmp_path)
        manifest = load_manifest_dict(_minimal_dict())
        result = load_plugin(manifest, plugin_dir=tmp_path)
        assert "net.fetch" in result.capabilities

    def test_signed_false_when_unsigned(self, tmp_path: Path) -> None:
        _write_plugin_module(tmp_path)
        manifest = load_manifest_dict(_minimal_dict())
        result = load_plugin(manifest, plugin_dir=tmp_path)
        assert result.signed is False
