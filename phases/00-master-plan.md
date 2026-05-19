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

- ✅ Done: P-01, P-02, P-03, P-04
- ⏳ Active: P-05 — Mid-turn context compression (next)
- ⏸ Pending: P-06 .. P-100

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
