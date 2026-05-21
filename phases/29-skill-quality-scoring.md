# P-29 — Skill quality scoring

## Status

done (shipped 2026-05-21, this session). TDD vertical-slice loop.

## Outclass claim

**Live quality scores in `sera skills`** — usage, success %, cost, user thumbs. Bad skills demote themselves out of the suggestion list without manual curation. Rivals pick skills statically or by keyword match.

## Goal

Bad skills demote themselves out of the suggestion list.

## Deliverables

- `sera/skills/scoring.py`:
  - `SkillScore(name, invocations, successes, failures, total_cost, thumbs_up, thumbs_down)` — frozen dataclass.
  - `quality_score(s)` → float in [0, 1]: `0.7 × success_rate + 0.3 × thumb_factor`. New skills (no invocations) score 1.0 (benefit of doubt). Bounded.
  - `DEFAULT_SUGGEST_THRESHOLD = 0.35`.
  - `SkillScorer(db_path)` — SQLite-backed tracker. Methods: `record_invocation`, `record_success`, `record_failure`, `record_cost`, `thumbs_up`, `thumbs_down`, `get`, `score_of`, `should_suggest`, `demoted_skills`, `all_scores`. UPSERT via `ON CONFLICT`.
- `sera/cli/main.py`:
  - `sera skills scores [--db PATH] [--threshold N]` — Rich table ranked by score; demoted skills flagged.

## Files touched

new `sera/skills/scoring.py`; edit `sera/cli/main.py` (1 new subcommand); new `tests/test_skill_scoring.py` (18 tests).

## Verification

```bash
pytest -q tests/test_skill_scoring.py       # 18 passed
pytest -q                                    # 466 passed total (was 448 + 18 new)
python -m pyflakes sera/                     # 0 warnings
```

Phase verification clause: `test_three_failures_drops_below_threshold` — 3 invocations + 3 failures → `should_suggest` returns `False`.

## Dependencies

P-26.

## Notes

**TDD vertical-slice loop (4 cycles, RED→GREEN each):**

1. RED→GREEN: `quality_score()` math — perfect skill = 1.0; all-fail = < 0.2; no invocations = 1.0; thumbs_down lowers; thumbs_up raises; bounded [0, 1].
2. RED→GREEN: `SkillScorer` SQLite store — record events, `get` returns `SkillScore`, cost accumulates, thumbs persist.
3. RED→GREEN: `should_suggest`, `demoted_skills`, recovery path (enough successes can un-demote).
4. RED→GREEN: `sera skills scores` CLI — empty table + populated table.

**Design decisions (2026-05-21):**

- **0.7 / 0.3 success/thumb split.** Success rate is the ground truth; thumb signal is user override. 70/30 lets user feedback nudge but not override objective outcomes.
- **Benefit of doubt at 1.0 (not threshold).** New skills start at full score, decay only on observed failures. Bias toward showing; let outcomes demote.
- **thumb_factor at 0.5 when no thumbs.** Neutral starting point — neither positive nor negative signal. Combined with success_rate=1.0, new skill scores exactly 1.0.
- **SQLite UPSERT not UPDATE.** Single SQL statement handles both insert-on-first-touch and increment-on-existing without a read-modify-write race.
- **`demoted_skills()` scans all tracked skills.** Only tracked skills can be demoted — unknown skills are assumed new and get benefit of doubt.
