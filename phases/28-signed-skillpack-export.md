# P-28 — Signed `.skillpack` export

## Status

done (shipped 2026-05-21, this session). TDD vertical-slice loop.

## Outclass claim

**Signature verification on import.** Hermes ships unsigned `.md` only. Sera's `.skillpack` carries an Ed25519 signature over the content manifest — any post-export tamper (content, hash, or signature) is rejected on import.

## Goal

Skills travel between machines with tamper-evident packaging.

## Deliverables

- `sera/skills/pack.py`:
  - `PackError(RuntimeError)` — surfaces format/sig/tamper failures.
  - `generate_keypair()` → `(private_key_pem, public_key_pem)` — Ed25519 via `cryptography` package.
  - `pack_skill(skills_dir, name, out_path, *, private_key_pem=None)` — zip `SKILL.md` + `manifest.json` (SHA256 per file) + optional `SIGNATURE.b64`.
  - `sign_pack(pack_path, private_key_pem)` — add/replace `SIGNATURE.b64` (signs `manifest.json` bytes).
  - `verify_pack(pack_path, public_key_pem)` — verify Ed25519 sig; raises `PackError` on bad/missing sig.
  - `unpack_skill(pack_path, skills_dir, *, public_key_pem=None)` — verify hashes → optional sig check → extract to `<skills_dir>/<name>/SKILL.md`. Returns skill name.
- `sera/cli/main.py`:
  - `sera skills export <name> [--out PATH] [--key KEYFILE]` — pack + optionally sign.
  - `sera skills import <path> [--key PUBKEYFILE]` — unpack + optionally verify sig.

## Files touched

new `sera/skills/pack.py`; edit `sera/cli/main.py` (2 new subcommands); new `tests/test_skill_pack.py` (15 tests).

## Verification

```bash
pytest -q tests/test_skill_pack.py       # 15 passed
pytest -q                                # 448 passed total (was 433 + 15 new)
python -m pyflakes sera/                 # 0 warnings
```

Phase verification clause: `test_verify_fails_on_post_sign_tamper` — sign a pack, corrupt `manifest.json` inside the zip, `verify_pack` raises `PackError("signature")`. CLI equivalent: `test_cli_skills_import_rejects_bad_sig`.

## Dependencies

P-21, P-27.

## Notes

**TDD vertical-slice loop (4 cycles, RED→GREEN each):**

1. RED→GREEN: `pack_skill` creates zip with `SKILL.md` + `manifest.json` (SHA256); `unpack_skill` restores + verifies hashes; tampered content → `PackError("hash mismatch")`.
2. RED→GREEN: `generate_keypair` → Ed25519 PEM pair; `sign_pack` → `SIGNATURE.b64`; `verify_pack` succeeds with correct key, fails with wrong key, fails when unsigned.
3. RED→GREEN: post-sign tamper (forged manifest hash) fails `verify_pack`; `unpack_skill(public_key_pem=...)` path verifies before extracting.
4. RED→GREEN: `sera skills export` + `sera skills import` CLI end-to-end; export with `--key` signs; import with wrong pubkey exits non-zero.

**Design decisions (2026-05-21):**

- **`cryptography` package over stdlib.** Python stdlib has no Ed25519. `cryptography` (PyCA) is the standard, widely-installed package; it's already present on any machine doing TLS. Optional import with `PackError` on absence — the rest of Sera works without signing.
- **Sign the manifest, not the zip.** The zip format makes signing the bytes stream impractical (zip rewriting changes offsets). Signing `manifest.json` is clean: the manifest contains SHA256 of every content file, so any tamper to any file breaks the hash, and any tamper to the manifest itself breaks the signature. Two-layer protection.
- **`SIGNATURE.b64` inside the zip.** Not a sidecar file — the pack is self-contained. One file to email/copy/ship.
- **`unpack_skill` verifies sig before extracting.** Fail fast before writing anything to disk.
- **Graceful degradation.** Packs without `SIGNATURE.b64` unpack fine unless the caller supplies `public_key_pem`. Callers that don't care about provenance pay no runtime cost.
