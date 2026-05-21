# P-25 — Skill replay verification

## Status

done (shipped 2026-05-21, this session). TDD vertical-slice loop.

## Outclass claim

**Replay-promoted skills.** A skill cannot move out of CANDIDATE until every captured replay case passes. Hermes promotes by lifecycle (age, usage); Sera promotes by **correctness**. Broken skill → stays candidate → not registered as a tool → can't reach the agent's working context.

## Goal

Bad skills can't reach users.

## Deliverables

- `sera/skills/lifecycle.py`:
  - `skill_lifecycle.verified INTEGER NOT NULL DEFAULT 1` column + idempotent ALTER migration. Default = 1 keeps pre-P-25 rows verified so the upgrade doesn't silently unverify everyone.
  - `mark_candidate(name)` — flips verified to 0; upserts if missing.
  - `verify(name)` — flips verified to 1.
  - `is_verified(name)` — unknown names read as verified (back-compat).
  - `LifecycleRow.verified` boolean field.
- `sera/skills/loader.py`:
  - `SkillRegistry._is_runtime_eligible(name)` — combined gate: not archived AND (verified OR pinned). Pin overrides verification (user-asserted trust); refresh skips ineligible skills + unregisters them if they had been live.
- `sera/skills/verify.py`:
  - `ReplayCase(id, input, expect_substring?, expect_equals?)`, `ReplayResult(case_id, passed, reason, output)`, `VerificationReport(skill_name, results)` with `passed` / `n_passed` / `n_failed` properties.
  - `replay_tool(tool, case)` — invokes the handler, catches exceptions, scores against expectations.
  - `replay_skill(skill, case)` — wrapper that builds the Tool via `skill_to_tool`.
  - `verify_via_replay(lifecycle, skill, cases)` — runs every case; flips `lifecycle.verify(name)` iff *every* case passes. Empty cases never promote.
  - `load_replay_cases(path)` / `load_replay_skill_name(path)` — YAML loaders for `tests/skill_replay/*.yaml`.
- `tests/skill_replay/{caveman,egoist}.yaml` — bundled replay traces for the two text-only sample skills; end-to-end test verifies caveman against its real SKILL.md.

## Files touched

new `sera/skills/verify.py`; edit `sera/skills/lifecycle.py`, `sera/skills/loader.py`; new `tests/skill_replay/caveman.yaml` + `tests/skill_replay/egoist.yaml`; new `tests/test_skill_verify.py` (21 tests).

## Verification

```bash
pytest -q tests/test_skill_verify.py    # 21 passed
pytest -q                                # 398 passed total (was 377 + 21 new)
python -m pyflakes sera/                 # 0 warnings
```

Phase verification clause met: `test_verify_via_replay_keeps_candidate_when_any_fails` — broken skill stays candidate, `is_verified` remains False. `test_registry_skips_unverified_candidate` — the candidate never reaches the tool registry.

## Dependencies

P-24.

## Notes

_Journal: decisions, blockers, commit refs go here._

**TDD vertical-slice loop (6 cycles, RED→GREEN each):**

1. RED→GREEN: `verified` column + `mark_candidate` + `is_verified`; unknown reads as True; explicit candidate flips False.
2. RED→GREEN: `verify(name)` roundtrip; `LifecycleRow.verified` exposed.
3. RED→GREEN: `SkillRegistry` skips unverified candidates; pin override re-admits them.
4. RED→GREEN: `ReplayCase` + `replay_skill` / `replay_tool`; substring + equals expectations; handler crash → failing result.
5. RED→GREEN: `verify_via_replay` flips lifecycle iff all-pass; empty cases never promote.
6. RED→GREEN: YAML loader + bundled `caveman` + `egoist` traces; end-to-end real-skill verify.

**Design decisions (2026-05-21):**

- **`verified` defaults to 1.** Migrating a pre-P-25 DB must not silently disable every existing skill. New skills explicitly call `mark_candidate(name)`; the curator (P-23) is the natural caller in the wired flow.
- **Pin overrides verification.** A user explicitly pinning a skill is asserting trust — the candidate gate should defer. `_is_runtime_eligible` checks `verified OR pinned` so pinned candidates still register. (Archive still blocks — opting out of the world is unambiguous.)
- **Empty case list never promotes.** Phase verification depends on this: a skill with no captured replay can't be promoted by accident or by a tooling bug that returns an empty test set. `verify_via_replay([])` returns `passed=False`.
- **Handler exceptions become failing results.** `replay_tool` catches anything the handler raises and wraps it in a `ReplayResult(passed=False, reason="handler raised: ...")`. The verifier must never crash on a misbehaving skill — that's exactly the kind of skill it's designed to filter out.
- **`expect_substring` + `expect_equals` are the v1 vocabulary.** Anything richer (regex, JSON-schema, multi-turn replay against a real LLM) is future work. The two checks cover the dominant skill-output shapes: free-text bodies, exact stub responses.
- **`replay_skill` builds a fresh Tool per call.** Doesn't mutate the live registry. Replay is read-only from the runtime's perspective — pure verification, no side effects beyond the lifecycle flip.
- **Bundled replay traces co-located with bundled SKILL.md.** `tests/skill_replay/<name>.yaml` mirrors `tests/eval_cases/skills/<name>/SKILL.md`. End-to-end test wires them together so the bundled three remain a coherent reference set (not three unrelated examples).
- **Pin-then-mark-candidate-then-archive ordering covered.** P-24's archive-clears-pin contract still holds because the candidate gate is checked *after* archive — runtime path is archive → eligibility → verification, and archive short-circuits before pin is consulted.
- **Lazy import in `_is_runtime_eligible`.** Kept `sera.skills.loader` from pulling `sera.skills.lifecycle` at import time. P-24 already paid that cost; P-25 doesn't widen the cycle.
- **Test stub uses pin+candidate combo.** `test_pinned_candidate_still_registers` proves the override path. A user dropping a half-baked skill into `~/.sera/skills/` and `sera skills --pin name` keeps the productivity flow when they trust the source themselves.
