# P-47 — Plugin manifest spec

## Status

done.

## Outclass claim

**Permissions declared in manifest** — ClawHub-style but signed.

## Goal

Third parties extend Sera safely.

## Files

`sera/plugins/manifest.py`.

## Verification

hand-written plugin loads + tools register without core changes.

## Dependencies

P-22.


## Notes

2026-05-23: `sera/plugins/manifest.py` — PluginManifest dataclass (name/version/entrypoint/author/description/capabilities/tools/signature); ToolEntry (name/attr/permission/description/parameters); SignatureBlock (algorithm/public_key/signature). Capability enum: net.fetch, net.serve, fs.read, fs.write, shell.execute, tools.register, mcp.connect, composio.connect, browser.use, llm.call. parse_capability("fs.read:/tmp")→(root,scope). canonical_payload(): sorted-keys JSON minus signature for stable signing. sign_manifest(priv_bytes)→SignatureBlock (Ed25519 from cryptography lib). verify_signature() returns False on missing/invalid, raises on unsupported algorithm. load_plugin(manifest, plugin_dir, require_signature): imports entrypoint, getattr each tool's attr, registers as plugin_<name>__<tool>. unload_plugin() reverses. Verification: hand-written plugin.yaml + Python module load, get_weather tool registers + runs without core changes. Tampered manifest fails verification. 32 tests, 924 total.
