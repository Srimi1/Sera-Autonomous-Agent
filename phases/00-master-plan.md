# 00 — Master Plan

The 100-phase Sera roadmap lives in [`../STEP-BY-STEP.md`](../STEP-BY-STEP.md).

Single source of truth. This `phases/` folder contains one slim mirror file per phase, kept in sync with that document.

## Quick map

| Epoch | Phases | Theme |
|---|---|---|
| 1 | 01–10 | Foundation Hardening |
| 2 | 11–20 | Memory & Knowledge |
| 3 | 21–30 | Skill Mind & Curator |
| 4 | 31–40 | Council & Learned Routing |
| 5 | 41–50 | Tools, Sandbox, Tool-Gen |
| 6 | 51–60 | Multi-Channel Gateway |
| 7 | 61–70 | Desktop Body (Tauri) |
| 8 | 71–80 | Self-Improvement Engine |
| 9 | 81–90 | Defence & Eval |
| 10 | 91–100 | Moonshots |

## Status snapshot

- ✅ Suite green: **2021 passed, 0 failed.** P-48 runtime tool-gen fixed (dry-run subprocess now resolves `sera` via baked repo-root) — the flagship self-extension outclass passes its own e2e tests.
- ✅ Done (proven): ~62 phases — mechanism executes in passing tests (encrypted approval vault, causal-edge graph, CRDT merge, Ed25519 packs, audit chain, council, chaos monkey, …)
- 📦 Deferred to native toolchain (~22): Tauri desktop/mobile (P-61/65/66/67/95), voice binaries (P-68/69), real local models + mlx LoRA (P-73/74/93), GitHub branch-protection apply (P-90), Rust compile (P-98), native installer + 5-min timing (P-99). Python seams complete + tested; real capability unproven until the toolchain runs.
- 🟡 Honesty pass: P-81 re-headlined (heuristics matching DistilBERT-class recall, no phantom .onnx); P-98 downgraded to `scaffolded` (3× is a target, not a measurement — Rust uncompiled).

## Workflow

1. Pick the lowest-numbered `pending` phase whose dependencies are all `done`.
2. Read its phase file + the matching `### P-NN` block in `STEP-BY-STEP.md`.
3. Implement. Tests + verification command must pass.
4. Flip Status to `done` in the phase file. Add notes (commit refs, decisions).
5. Run the `save-phase` skill or update by hand.

## Regenerate phase files

```bash
python scripts/gen_phases.py
```

Re-runs are idempotent. Manual edits to the Notes section are overwritten — keep journal entries in `STEP-BY-STEP.md` or in commit messages.
