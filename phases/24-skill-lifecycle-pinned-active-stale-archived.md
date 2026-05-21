# P-24 — Skill lifecycle (pinned / active / stale / archived)

## Status

done (shipped 2026-05-21, this session). TDD vertical-slice loop.

## Outclass claim

**Recovery from archive.** Skills are never deleted; archived skills can be revived by user or curator. `revive(name)` flips an archived row back to ACTIVE with one call. Rivals either delete on stale or let dead manifests clutter the registry; Sera keeps the bytes and the audit trail intact.

## Goal

Auto-transition by access freshness + verification status.

## Deliverables

- `sera/skills/lifecycle.py`:
  - `LifecycleState` enum: `PINNED`, `ACTIVE`, `STALE`, `ARCHIVED`.
  - `STALE_AFTER_SECONDS = 90 days`, `ARCHIVE_AFTER_SECONDS = 180 days`.
  - `SkillLifecycle(db_path=~/.sera/skills_lifecycle.db)`:
    - `upsert(name, now?)` — idempotent ACTIVE+now insert; no-op when row exists.
    - `touch(name, now?)` — bumps `last_used_at`; recovers STALE → ACTIVE; preserves ARCHIVED (a touch doesn't accidentally unbury archive).
    - `get(name)` — raw `LifecycleRow | None`.
    - `state_of(name, now?)` — pure read; ARCHIVED is sticky; PINNED wins; otherwise time-decayed.
    - `pin(name, now?)` / `unpin(name)` — pinned skills skip every auto-transition.
    - `archive(name, now?)` — explicit; sets state + `archived_at`; clears `pinned` (user opted out).
    - `revive(name, now?)` — ARCHIVED → ACTIVE; clears `archived_at`; updates `last_used_at`.
    - `sweep(now?)` — applies ACTIVE → STALE auto-transition; returns `SweepSummary(transitions_to_stale, proposed_archives)`. Archive never auto-applies — it's surfaced for user prompt.
  - `SweepSummary` dataclass.
- `sera/skills/loader.py`:
  - `SkillRegistry(root, *, lifecycle=None)` — when a lifecycle is supplied, refresh skips ARCHIVED skills (treats them as deleted from the runtime view) and `touch()`es the lifecycle row on every successful register / update. No-lifecycle mode preserves P-22 behavior.

## Files touched

new `sera/skills/lifecycle.py`; edit `sera/skills/loader.py`; new `tests/test_skills_lifecycle.py` (23 tests).

## Verification

```bash
pytest -q tests/test_skills_lifecycle.py    # 23 passed
pytest -q                                    # 377 passed total (was 354 + 23 new)
python -m pyflakes sera/                     # 0 warnings
```

Phase verification clause: `test_idle_90_days_reads_as_stale` (skill idle 90d transitions to stale via `state_of`) + `test_sweep_does_not_auto_archive` (archive surfaces as proposal, not auto-applied → user-prompt contract).

## Dependencies

P-23.

## Notes

_Journal: decisions, blockers, commit refs go here._

**TDD vertical-slice loop (7 cycles, RED→GREEN each):**

1. RED→GREEN: `LifecycleState` enum + `SkillLifecycle.upsert/state_of` tracer; unseen reads ACTIVE.
2. RED→GREEN: `touch` bumps `last_used_at`; first-touch creates row; `get` returns row or None.
3. RED→GREEN: idle 90d → STALE; just-under-90d still ACTIVE; touch recovers from STALE.
4. RED→GREEN: `sweep` returns archive proposals past 180d; never auto-archives; idempotent across re-sweeps.
5. RED→GREEN: `pin`/`unpin`; PINNED never auto-transitions; pin-then-unpin lets decay resume.
6. RED→GREEN: `archive` flips state + records timestamp; ARCHIVED is sticky; `revive` returns ARCHIVED → ACTIVE.
7. RED→GREEN: `SkillRegistry` integration — refresh skips ARCHIVED skills, touches lifecycle on register, revived skill re-registers on next refresh.

**Design decisions (2026-05-21):**

- **Two-table-equivalent in one schema.** `skills_lifecycle` has `pinned INTEGER` *and* `state TEXT`. Pinned is a flag, not a state — a pinned skill is also conceptually ACTIVE (its tool is registered). Keeping both fields lets `pin()` be independent of state transitions and `archive()` survive past `pin()` if the user changes their mind.
- **STALE auto-applies; ARCHIVE doesn't.** Stale is reversible by use (touch returns it to ACTIVE). Archive removes the skill from search results — that's a destructive-feeling action even though no data is lost. Confirmation prompt is the contract; `sweep()` returns the candidate list to whichever surface asks (CLI now, curator-of-curators later).
- **`state_of` is read-only.** Time-based decay is computed at read time, not written back. The persisted `state` column only changes on explicit transitions (`sweep`, `archive`, `revive`, `pin`/`unpin`). Means a 90-day-old read reflects truth even if `sweep` hasn't run yet — no "stale unless sweep ran" footgun.
- **ARCHIVED is sticky.** A `touch` on an archived row does *not* unbury it — only `revive` does. This prevents accidental restoration when a session happens to mention the archived skill name. Test `test_archived_is_sticky_against_decay` locks the contract.
- **`archive` clears pin.** A pinned skill being explicitly archived is the user stating new intent. Silently keeping `pinned=1` would create a weird "pinned-but-hidden" state. Clear it; if the user wants pin back after revive, they can re-pin.
- **Lifecycle param is opt-in on SkillRegistry.** P-22 tests still pass because `lifecycle=None` mode short-circuits the archive check + lifecycle touches. New integration tests inject a lifecycle to verify the wired behaviour. Backward-compat by construction.
- **Lazy import of `LifecycleState` inside `_is_archived`.** `sera.skills.lifecycle` would otherwise pull `sera.skills.loader` indirectly (via lifecycle code that wants to query the registry). Lazy import keeps each module's top-level imports clean and avoids a circular dependency.
- **Verification status is out of scope.** Phase claim mentions "freshness + verification status." This shipped freshness; verification (skill self-tests / eval scores) is a P-29-ish hook. Resist horizontal-slice temptation — ship the freshness signal cleanly, layer verification later.
- **No CLI verbs yet.** Skeleton ships the API + auto-transition pipeline. `sera skills pin|archive|revive` CLI verbs are one click decorator per command — bundling them with curator-of-curators makes a coherent next slice rather than 4 single-line phase doc bumps. Defer.
- **30/60/90 day choices documented as constants, not buried.** A future tuning pass (e.g. 60/120 for solo users) flips two top-level constants; the test fixtures use the constants by name (`STALE_AFTER_SECONDS`, `ARCHIVE_AFTER_SECONDS`) so the contract stays consistent.
