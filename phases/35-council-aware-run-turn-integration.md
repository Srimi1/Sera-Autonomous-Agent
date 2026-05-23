# P-35 — Council-aware run_turn integration

## Status

done.

## Outclass claim

**Per-skill council opt-in.** A skill marked `council: true` triggers ensemble for that single tool call only.

## Goal

No global toggle; council is surgical.

## Files

`sera/agent/loop.py`, `sera/skills/manifest.py`.

## Verification

skill with council:true uses ensemble; without, single model.

## Dependencies

P-34, P-22.


## Notes

2026-05-22. `sera/skills/manifest.py` + `sera/agent/loop.py` delta shipped. `CouncilConfig` holds models, factory, council_skills set, synthesis_model_id. `run_turn` gets optional `council_config` param; skill tool calls in `council_skills` route through 4-step council pipeline (answers → rankings → confidence → chairman synthesis) instead of normal dispatch. Zero overhead for non-council skills. `council_skills_from_disk` helper for CLI init. 15 tests, full suite 626/626.
