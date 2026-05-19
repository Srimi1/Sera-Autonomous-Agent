# P-04 — Egoist skill + phases/ folder bootstrap

## Status

done (shipped 2026-05-19, this session).

## Outclass claim

**The Egoist mindset locked into a skill** — every Sera session loads it. Discipline as a primitive, not a vibe.

## Goal

Lay the planning infrastructure: the egoist skill is installed globally, the `phases/` folder exists with one markdown per phase, and `STEP-BY-STEP.md` mirrors this plan inside the repo so we both work from the same guardrail.

## Deliverables

- `~/.claude/skills/egoist/SKILL.md` written with the exact text from the "The Egoist" section above.
  - `Project_sera/phases/` directory created.
  - `Project_sera/STEP-BY-STEP.md` mirroring this plan file (full content).
  - `Project_sera/phases/00-master-plan.md` — short pointer to `STEP-BY-STEP.md`.
  - 100 phase files: `phases/01-package-scaffold.md` through `phases/100-public-ship.md`. Each contains: `## Status / ## Outclass / ## Goal / ## Deliverables / ## Files / ## Verification / ## Dependencies / ## Notes`. Content extracted directly from this plan.
  - Phase files 01-03 marked `done` with the shipped-today claims and exact verification commands. Phase 04 marked `done` at end of this phase. Phases 05-100 marked `pending`.
  - `README.md` updated: replace "Week 1 status" pointer with a single line linking to `STEP-BY-STEP.md`.

## Files touched

new `~/.claude/skills/egoist/SKILL.md`; new `Project_sera/phases/*.md` × 101; new `Project_sera/STEP-BY-STEP.md`; edit `Project_sera/README.md`.

## Verification

```bash
  cat ~/.claude/skills/egoist/SKILL.md | head -5
  ls "Project_sera/phases/" | wc -l        # expect: 101
  test -f Project_sera/STEP-BY-STEP.md
  pytest -q                                # expect: still 14 passed (no code changed)
  ```

## Dependencies

P-03.

## Notes

No production code changes in this phase. Pure scaffolding for the discipline that runs P-05 onward.


## Notes

_Journal: decisions, blockers, commit refs go here._
