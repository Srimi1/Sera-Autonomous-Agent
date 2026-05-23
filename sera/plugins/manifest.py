"""Plugin manifest spec — permissions declared up-front, optionally signed.

Outclass: ClawHub-style permission declarations with Ed25519 signatures over the
canonical manifest. Third-party plugins extend Sera safely: tools register via
the manifest without modifying core, and the user reviews capabilities at install.

Manifest format (plugin.yaml):

    name: weather-tools
    version: 1.0.0
    author: alice <alice@example.com>
    description: Real-time weather queries
    entrypoint: weather_plugin

    capabilities:
      - net.fetch
      - tools.register

    tools:
      - name: get_current_weather
        attr: get_current_weather
        permission: READ_ONLY
        description: Fetch current weather for a city.
        parameters:
          type: object
          properties:
            city: { type: string }
          required: [city]

    signature:                # optional but recommended
      algorithm: ed25519
      public_key: <base64>
      signature: <base64>     # over canonical_payload(manifest)
"""
from __future__ import annotations

import base64
import enum
import hashlib
import importlib
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from sera.tools.base import Permission, Tool, ToolContext, ToolScope
from sera.tools.registry import register, unregister


# ---------------------------------------------------------------------------
# Capability vocabulary — what a plugin can claim
# ---------------------------------------------------------------------------

class Capability(enum.Enum):
    NET_FETCH      = "net.fetch"        # outbound HTTP
    NET_SERVE      = "net.serve"        # open ports
    FS_READ        = "fs.read"          # read files (may include :<path> scope)
    FS_WRITE       = "fs.write"         # write files
    SHELL_EXEC     = "shell.execute"    # run shell commands
    TOOLS_REGISTER = "tools.register"   # register new tools
    MCP_CONNECT    = "mcp.connect"      # connect to MCP servers
    COMPOSIO       = "composio.connect" # use Composio actions
    BROWSER        = "browser.use"      # drive the Playwright browser
    LLM_CALL       = "llm.call"         # invoke LLMs


KNOWN_CAPABILITY_ROOTS: frozenset[str] = frozenset(c.value.split(":")[0] for c in Capability)


def parse_capability(raw: str) -> tuple[str, str]:
    """Split 'fs.read:/tmp' into ('fs.read', '/tmp'). Bare roots → ('fs.read', '')."""
    if ":" in raw:
        root, scope = raw.split(":", 1)
    else:
        root, scope = raw, ""
    return root.strip(), scope.strip()


def validate_capabilities(caps: list[str]) -> list[str]:
    """Return the subset of caps that are not recognised."""
    bad: list[str] = []
    for raw in caps:
        root, _scope = parse_capability(raw)
        if root not in KNOWN_CAPABILITY_ROOTS:
            bad.append(raw)
    return bad


# ---------------------------------------------------------------------------
# Manifest schema
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ToolEntry:
    name: str
    attr: str                  # attribute name on the entrypoint module
    permission: str            # READ_ONLY | WRITE | EXECUTE | DANGEROUS
    description: str = ""
    parameters: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {}})


@dataclass(frozen=True)
class SignatureBlock:
    algorithm: str        # "ed25519"
    public_key: str       # base64
    signature: str        # base64, over canonical_payload(manifest)


@dataclass(frozen=True)
class PluginManifest:
    name: str
    version: str
    entrypoint: str       # importable module path, e.g. "weather_plugin"
    author: str = ""
    description: str = ""
    capabilities: tuple[str, ...] = ()
    tools: tuple[ToolEntry, ...] = ()
    signature: SignatureBlock | None = None


class ManifestError(Exception):
    """Manifest schema, signature, or load failure."""


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

_REQUIRED_FIELDS: frozenset[str] = frozenset({"name", "version", "entrypoint"})


def load_manifest_dict(raw: dict[str, Any]) -> PluginManifest:
    missing = _REQUIRED_FIELDS - raw.keys()
    if missing:
        raise ManifestError(f"missing required fields: {sorted(missing)}")

    caps = list(raw.get("capabilities", []) or [])
    bad = validate_capabilities(caps)
    if bad:
        raise ManifestError(f"unknown capabilities: {bad}")

    tools_raw = raw.get("tools", []) or []
    tool_entries: list[ToolEntry] = []
    for t in tools_raw:
        if not all(k in t for k in ("name", "attr", "permission")):
            raise ManifestError(f"tool entry missing fields: {t}")
        try:
            Permission.parse(t["permission"])
        except ValueError as e:
            raise ManifestError(f"invalid permission for tool {t['name']!r}: {e}") from e
        tool_entries.append(ToolEntry(
            name=t["name"],
            attr=t["attr"],
            permission=t["permission"],
            description=t.get("description", ""),
            parameters=t.get("parameters", {"type": "object", "properties": {}}),
        ))

    sig_raw = raw.get("signature")
    signature = None
    if sig_raw:
        if not all(k in sig_raw for k in ("algorithm", "public_key", "signature")):
            raise ManifestError(f"signature block missing fields: {sig_raw}")
        signature = SignatureBlock(
            algorithm=sig_raw["algorithm"],
            public_key=sig_raw["public_key"],
            signature=sig_raw["signature"],
        )

    return PluginManifest(
        name=raw["name"],
        version=raw["version"],
        entrypoint=raw["entrypoint"],
        author=raw.get("author", ""),
        description=raw.get("description", ""),
        capabilities=tuple(caps),
        tools=tuple(tool_entries),
        signature=signature,
    )


def load_manifest_file(path: Path | str) -> PluginManifest:
    """Read and parse a plugin.yaml manifest."""
    p = Path(path)
    if not p.exists():
        raise ManifestError(f"manifest not found: {p}")
    with p.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    if not isinstance(raw, dict):
        raise ManifestError(f"manifest root must be a mapping, got {type(raw).__name__}")
    return load_manifest_dict(raw)


# ---------------------------------------------------------------------------
# Canonical payload + signatures
# ---------------------------------------------------------------------------

def canonical_payload(manifest: PluginManifest) -> bytes:
    """Stable byte representation of manifest used for signing.

    The signature field is excluded. Field order is fixed and values are
    serialized as compact JSON with sorted keys — bit-for-bit reproducible.
    """
    body = asdict(manifest)
    body.pop("signature", None)
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


def manifest_digest(manifest: PluginManifest) -> str:
    """SHA-256 hex digest of the canonical payload — short fingerprint."""
    return hashlib.sha256(canonical_payload(manifest)).hexdigest()


def sign_manifest(manifest: PluginManifest, private_key_bytes: bytes) -> SignatureBlock:
    """Produce an Ed25519 signature block for the manifest's canonical payload."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization

    priv = Ed25519PrivateKey.from_private_bytes(private_key_bytes)
    pub = priv.public_key()
    pub_bytes = pub.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    sig = priv.sign(canonical_payload(manifest))
    return SignatureBlock(
        algorithm="ed25519",
        public_key=base64.b64encode(pub_bytes).decode("ascii"),
        signature=base64.b64encode(sig).decode("ascii"),
    )


def verify_signature(manifest: PluginManifest) -> bool:
    """Verify the manifest's signature. False on missing or invalid signature."""
    if manifest.signature is None:
        return False
    sig = manifest.signature
    if sig.algorithm != "ed25519":
        raise ManifestError(f"unsupported signature algorithm: {sig.algorithm}")
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        from cryptography.exceptions import InvalidSignature

        pub_bytes = base64.b64decode(sig.public_key)
        sig_bytes = base64.b64decode(sig.signature)
        pub_key = Ed25519PublicKey.from_public_bytes(pub_bytes)
        pub_key.verify(sig_bytes, canonical_payload(manifest))
        return True
    except InvalidSignature:
        return False
    except Exception as e:  # noqa: BLE001
        raise ManifestError(f"signature verification failed: {e}") from e


# ---------------------------------------------------------------------------
# Loader — imports entrypoint and registers tools
# ---------------------------------------------------------------------------

@dataclass
class LoadResult:
    plugin_name: str
    tool_names: list[str]
    capabilities: list[str]
    signed: bool


def _attr_to_tool(entry: ToolEntry, attr: Any, plugin_name: str) -> Tool:
    """Wrap a plugin function/coroutine as a Sera Tool."""
    import asyncio

    permission = Permission.parse(entry.permission)
    tool_name = f"plugin_{plugin_name}__{entry.name}"

    async def _handler(args: dict[str, Any], ctx: ToolContext) -> str:
        if asyncio.iscoroutinefunction(attr):
            result = await attr(args, ctx)
        else:
            result = attr(args, ctx)
        return str(result)

    return Tool(
        name=tool_name,
        description=f"[Plugin:{plugin_name}] {entry.description}",
        parameters=entry.parameters,
        permission=permission,
        scope=ToolScope.INTEGRATION,
        handler=_handler,
    )


def load_plugin(
    manifest: PluginManifest,
    *,
    plugin_dir: Path | str | None = None,
    require_signature: bool = False,
) -> LoadResult:
    """Import the entrypoint module and register every declared tool.

    Args:
        manifest: parsed PluginManifest.
        plugin_dir: directory to prepend to sys.path so the entrypoint is importable.
        require_signature: refuse to load unsigned manifests.

    Returns:
        LoadResult with registered tool names.

    Raises:
        ManifestError: signature invalid, entrypoint missing, or tool attr missing.
    """
    signed = False
    if manifest.signature is not None:
        if not verify_signature(manifest):
            raise ManifestError(f"signature verification failed for {manifest.name}")
        signed = True
    elif require_signature:
        raise ManifestError(f"plugin {manifest.name} is unsigned but require_signature=True")

    added_to_path: str | None = None
    if plugin_dir is not None:
        p = str(Path(plugin_dir).resolve())
        if p not in sys.path:
            sys.path.insert(0, p)
            added_to_path = p

    try:
        try:
            module = importlib.import_module(manifest.entrypoint)
        except ImportError as e:
            raise ManifestError(f"cannot import entrypoint {manifest.entrypoint!r}: {e}") from e

        tool_names: list[str] = []
        for entry in manifest.tools:
            attr = getattr(module, entry.attr, None)
            if attr is None:
                raise ManifestError(
                    f"entrypoint {manifest.entrypoint!r} has no attr {entry.attr!r}"
                )
            if not callable(attr):
                raise ManifestError(
                    f"plugin attr {entry.attr!r} is not callable"
                )
            tool = _attr_to_tool(entry, attr, manifest.name)
            register(tool)
            tool_names.append(tool.name)

        return LoadResult(
            plugin_name=manifest.name,
            tool_names=tool_names,
            capabilities=list(manifest.capabilities),
            signed=signed,
        )
    except Exception:
        # Roll back the sys.path mutation on failure so a bad plugin can't pollute.
        if added_to_path and added_to_path in sys.path:
            sys.path.remove(added_to_path)
        raise


def unload_plugin(result: LoadResult) -> int:
    """Drop the plugin's registered tools. Returns count removed."""
    return sum(1 for name in result.tool_names if unregister(name))
