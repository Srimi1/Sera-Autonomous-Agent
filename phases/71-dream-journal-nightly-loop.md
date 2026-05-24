# P-71 — Dream Journal nightly loop

## Status

done. **(First teeth-phase of the autonomy track — fully verified here.)**

## Outclass claim

**Kimi's blueprint proposed nightly consolidation; nobody shipped it. Sera
ships it.** Each night the agent reviews the day's sessions and produces a
dream entry with three things rivals don't generate offline:

1. **Consolidation** — a narrative summary so tomorrow starts from distilled
   memory, not raw transcript.
2. **Candidate skills** — repeated tool patterns become drafted skills (via the
   P-30 DiscoveryAgent), queued for the A/B harness.
3. **Synthetic Q-A** — question/answer pairs distilled from real usage. This is
   the training corpus P-72 exports as JSONL and P-73 fine-tunes a local LoRA
   on. The flywheel: today's work → tomorrow's cheaper, sharper agent.

Offline and local. The whole loop is testable with a stub model + fake clock.

## Files

- `sera/dream/__init__.py`, `sera/dream/journal.py` — DreamJournal,
  DreamJournalStore, DreamEntry, SyntheticQA
- `sera/cli/main.py` — `sera dream` (shows recent entries)
- `tests/test_dream.py` — 11 tests

## Verification

| Check | Status | Notes |
|-------|--------|-------|
| 11 tests | ✅ | store, one-night dream, soft-failure, the 5-day verification |
| **5 days → 5 entries + ≥1 draft** | ✅ | test_five_days_five_entries_one_draft (the literal phase verification) |
| consolidation summary | ✅ | day's sessions → narrative |
| synthetic Q-A | ✅ | usage → {question, answer} pairs |
| candidate skill from repeated tool | ✅ | tool used ≥3× → drafted skill via DiscoveryAgent |
| below-threshold skips discovery LLM | ✅ | 2 uses → no proposal call (cost guard) |
| soft failure | ✅ | consolidation LLM error → entry still recorded |
| idempotent re-dream | ✅ | same date upserts, never duplicates |
| `sera dream` CLI | ✅ | registers + renders |
| full suite | ✅ | no regressions (1492 → 1503) |

## Limits

- **No real LLM consolidation** — tests use a stub `llm_call`; real nightly
  quality depends on a provider key. The orchestration, parsing, discovery
  gating, persistence, and 5-day verification are all real.
- **No scheduler wired** — `dream()` runs one night on demand; the cron that
  fires it at e.g. 3am isn't installed (would reuse the P-08 autofetch loop
  pattern). The nightly *logic* is complete and tested; the *trigger* is a thin
  follow-up.
- Synthetic Q-A is produced but **not yet exported** — that's P-72 (JSONL for
  mlx-lm/unsloth), which now has its dependency satisfied.

## Dependencies

P-30, P-15. Feeds P-72 (synthetic trace dataset) → P-73 (local LoRA).
