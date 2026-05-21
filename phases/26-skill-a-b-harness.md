# P-26 — Skill A/B harness

## Status

done (shipped 2026-05-21, this session). TDD vertical-slice loop.

## Outclass claim

**Cost × success-rate fitness — automatic pick of the cheaper-or-better variant, loser archived (not deleted).** Rivals pick one variant by author intent and ship it. Sera runs the ablation, surfaces the trade-off, lets cost break ties, and archives the loser per P-24 so the user can `lifecycle.revive(loser_name)` whenever they disagree.

## Goal

Two skill variants → ablation on a held-out set → winner kept.

## Deliverables

- `sera/skills/ab.py`:
  - `Variant(skill, cost)` — Skill + caller-supplied cost number (tokens / ms / $; unit-agnostic).
  - `ABResult(name, n_passed, total_cases, total_cost)` with `success_rate` derived property.
  - `Verdict(winner, loser, reason)`.
  - `compute_verdict(a, b)` — pure lex: `(success_rate desc, total_cost asc)`. Total tie → `a` wins (input-order stable).
  - `run_ab(a, b, cases)` — replays every case against both variants via `replay_skill`, sums passes, multiplies per-call cost by case count, returns `(ABResult, ABResult, Verdict)`.
  - `decide_and_persist(lifecycle, a, b, cases)` — runs A/B, calls `lifecycle.verify(winner)` + `lifecycle.archive(loser)` on success. **No-signal case** (both variants pass zero) leaves lifecycle untouched and returns the verdict for telemetry only.
- `sera/cli/main.py`:
  - `sera skills` converted to `invoke_without_command=True` group; default behavior (list / `--reload`) preserved.
  - New `sera skills ab --a PATH --b PATH --cases PATH --cost-a F --cost-b F [--lifecycle-db PATH] [--dry-run]` subcommand. Prints per-variant table, winner, loser-archive notice, verdict reason. `--dry-run` runs the harness without persisting.

## Files touched

new `sera/skills/ab.py`; edit `sera/cli/main.py` (skills→group + ab subcommand); new `tests/test_skill_ab.py` (11 tests).

## Verification

```bash
pytest -q tests/test_skill_ab.py        # 11 passed
pytest -q                                # 417 passed total (was 406 + 11 new)
python -m pyflakes sera/                 # 0 warnings
```

Phase verification clause: `test_run_ab_both_pass_picks_cheaper` — both variants pass every case, cheaper wins. `test_decide_and_persist_loser_recoverable_via_revive` — archived loser revives to ACTIVE.

## Dependencies

P-25.

## Notes

_Journal: decisions, blockers, commit refs go here._

**TDD vertical-slice loop (4 cycles, RED→GREEN each):**

1. RED→GREEN: `compute_verdict` lex math — higher success wins regardless of cost; tied success → cheaper wins; full tie → first arg wins (stable).
2. RED→GREEN: `Variant` + `run_ab` — replay aggregates, cost scales by case count, empty case list returns zero-pass + first-arg tie verdict.
3. RED→GREEN: `decide_and_persist` — lifecycle.verify(winner) + lifecycle.archive(loser); no-signal case (zero passes) leaves lifecycle untouched; archived loser revives cleanly.
4. RED→GREEN: `sera skills ab` CLI — end-to-end with two SKILL.md files + replay yaml; prints verdict + archive notice.

**Design decisions (2026-05-21):**

- **Cost is caller-supplied, unit-agnostic.** Sera doesn't pretend to know whether the user cares about tokens, wall ms, or dollars. The harness sums and compares — the user decides what the number means. P-26.5+ can wire a default cost extractor (token count of skill body × estimated calls per session) but the skeleton stays unopinionated.
- **Lex order, not weighted sum.** A weighted sum of `success_rate` and `cost` would hide the trade-off behind a magic coefficient. Lex says clearly: **success first, then cost**. Pareto-dominated variants lose; pareto-equivalent ones break by cost. Audit-friendly.
- **Stable tie-break (first arg wins).** Random tie-break sounds fair, isn't reproducible. Tests need deterministic verdicts. Callers wanting randomness shuffle arg order before calling.
- **No-signal case (zero passes both sides) is a silent skip.** Promoting either would be worse than promoting neither — both variants failed every assertion, so the user's replay cases are wrong or both variants are broken. Either way, the lifecycle stays in candidate, and the CLI surfaces the verdict (with rate 0%/0%) so the user can fix the replay set.
- **Loser archived, not deleted.** Per P-24's contract: bytes preserved, `archived_at` stamped, `revive(name)` flips back to ACTIVE. The user can resurrect any archived variant at any time. This is the outclass claim — no rival ships ablation-archive-with-revive.
- **CLI restructure to group, not separate top-level command.** `sera skills-ab` would have worked but `sera skills ab` reads better and shares the `--root` discovery context for future verbs (`sera skills archive`, `sera skills revive`, `sera skills pin`). Group conversion preserves both legacy invocations (`sera skills --root X`, `sera skills --root X --reload`) via `invoke_without_command=True`.
- **`--dry-run` flag is essential.** A/B with persistence is a destructive action (archive flip is reversible but the user wants to preview). `--dry-run` runs the math, prints the verdict, leaves the lifecycle untouched.
- **No per-case cost.** Cost is per-variant-per-call; the harness multiplies by `len(cases)` to get `total_cost`. P-26.5 could add per-case cost overrides (some cases are heavier than others) but the skeleton keeps the math obvious.
- **`Variant` is a dataclass, not a Protocol.** Two-field aggregation, no behavior — dataclass is the right primitive. Future expansion (per-variant rate limit, per-variant timeout) just adds fields.
- **Reuses P-25 `replay_skill`.** Every assertion is one already-tested call. No new score logic, no duplicated case parsing. A/B sits cleanly on top of replay verification; the two phases compose rather than overlap.

**What's deliberately deferred:**

- **Real-LLM A/B.** Skeleton runs the stub-skill handler (P-22's `skill_to_tool` returns the body verbatim). Real ablation against an LLM-driven skill executor is wired in P-30+. The math + persistence is the right surface to lock first.
- **Multi-way ablation.** A vs B vs C vs ... — straightforward extension to a list-of-Variant + reduce, but two-way is the dominant pattern and "champion vs challenger" matches the typical user workflow.
- **Statistical significance.** With 1-30 replay cases, A vs B differences of 1-2 passes are noise. P-26 promotes anyway because the user's replay set is the ground truth they explicitly authored. Significance tests land if/when sample sizes grow.
