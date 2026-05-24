# P-64 — Approval gate UI w/ encrypted vault

## Status

done. **(The Epoch 7 teeth-phase.)**

## Outclass claim

**Encrypted, tamper-evident shape-memory.** OpenHuman has an approval flow but
stores nothing encrypted. Sera's vault is AES-256-GCM at rest: every entry is
encrypted AND authenticated by the GCM tag, so you cannot hand-edit the file to
slip a dangerous call past the gate — any tampering fails authentication and
the load raises VaultError. The master key lives in the OS keychain (injectable
for tests), never on disk beside the ciphertext.

The shape-memory: "approve this exact (tool, arg-shape) once" is remembered so
the identical call auto-approves without re-prompting; a denial arms a 24h
cooldown that auto-denies the same shape. The fingerprint is SHA-256 over
(tool_name, canonical-JSON(args)) — approving `git status` never auto-approves
`git push`, and key-order doesn't matter.

## Files

- `sera/safety/vault.py` — EncryptedVault (AES-256-GCM), fingerprint,
  ApprovalRecord, secrets + approval shape-memory
- `sera/safety/approval.py` — VaultApprovalGate (drop-in ApprovalGate wrapping
  an inner prompt)
- `sera-shell/src/components/Approvals.tsx` — approval dialog (Allow / Deny /
  "always allow this shape")
- `tests/test_vault.py` — 26 tests

## Verification

| Check | Status | Notes |
|-------|--------|-------|
| 26 tests | ✅ | fingerprint, encryption, tamper-evidence, shape-memory, gate E2E |
| **Approve once → auto-approves** | ✅ | test_approve_once_then_auto_approves — 2nd identical call, inner prompted 0 extra times |
| **Deny → 24h cooldown** | ✅ | test_deny_arms_24h_cooldown — auto-deny within 24h; test_cooldown_expiry_reprompts — re-prompts after |
| Encrypted at rest | ✅ | secret + approved-command bytes absent from the file (test_file_is_not_plaintext, test_approval_memory_is_encrypted) |
| Tamper-evident | ✅ | flip one byte → VaultError (GCM auth fails); wrong key → VaultError |
| Exact-shape isolation | ✅ | different args / different tool → re-prompt, never cross-approve |
| Persists across process | ✅ | fresh vault + fresh gate honor the on-disk allow |
| Full suite | ✅ | No regressions |

## Limits

**What was NOT wired:**
- **Core↔shell approval transport.** The vault + gate are fully built and
  tested, and `Approvals.tsx` is written, but the round-trip — core pushes an
  `approval_requested` event to the shell over SSE and the shell POSTs a verdict
  to a `/v1/approval/respond` endpoint — is NOT yet wired. Today the Router
  dispatch path uses AutoApproveGate; swapping in VaultApprovalGate for the
  gateway/HTTP path (and adding the respond endpoint) is a follow-up. The
  *outclass* (encrypted shape-memory) is complete and verified at the Python
  layer; only the desktop transport is pending.
- **Approvals.tsx not executed** — no Tauri/Vite here; written as real code.
- **Vault key rotation / re-encrypt** — rotating the keychain master key
  orphans the existing vault (no re-encrypt-on-rotate). Acceptable for v1.
- **Single global vault file** — secrets and approvals share one blob; no
  per-namespace key separation.
- **Shape = exact args.** "Always allow `tail -n 50 X`" does not generalize to
  "any args to tail." Exact-match is the secure default; pattern/structural
  shapes (e.g. allow a tool with any path under ~/safe) are a future extension.

## Dependencies

P-61.
