# P-27 — Skill versioning + diff

## Status

done (shipped 2026-05-21, this session). TDD vertical-slice loop.

## Outclass claim

**Git-tracked skill history.** Every skill edit is a commit, attributed to its author (user / curator / custom), and walkable via `sera skills log <name>`. Combined with P-24's `revive()` + P-26's archive-on-loss, Sera ships **total recall + undo** on the skills layer — `git revert HEAD` on the `<skills_dir>/.git` repo plus `lifecycle.revive` recovers any state ever lived in. Rivals overwrite skill text in place.

## Goal

`sera skills log <name>` walks history.

## Deliverables

- `sera/skills/git.py`:
  - `GitNotAvailable(RuntimeError)` — surfaces missing `git` CLI cleanly; opt-in feature.
  - `ensure_repo(skills_dir)` — lazy `git init` + `.gitignore` (ignores `.lock` files from P-09's per-session lock pattern). Idempotent. Auto-creates `skills_dir` if missing. Sets `user.name=sera`/`user.email=sera@local` so headless commits work; per-commit `author=` override lets the curator attribute its edits.
  - `CommitInfo(sha, when, author, message)`.
  - `commit_skill_change(skills_dir, name, message, author=None)` — stages `<name>/SKILL.md`, detects no-op via `git diff --cached --quiet`, commits only on real change. Returns the new `CommitInfo`, or `None` when nothing was staged.
  - `skill_log(skills_dir, name, limit=20)` — `git log --pretty=format:%H%x00%at%x00%an <%ae>%x00%s -- <name>/SKILL.md`; NUL-separated parse so message text can't break the format. Newest first. Empty list when no history.
  - `skill_diff(skills_dir, name, ref_a=None, ref_b=None)` — defaults to `HEAD~1..HEAD` (latest edit); single-ref shortcut against HEAD; returns empty string when no history.
- `sera/cli/main.py`:
  - `sera skills commit <name> --message MSG [--author A]` — stage + commit one skill's manifest. Shares the group-level `--root` via `ctx.parent.params`.
  - `sera skills log <name> [--limit N]` — Rich table (sha[:8], when, author, message).
  - `sera skills diff <name> [--from REF] [--to REF]` — prints the manifest diff.

## Files touched

new `sera/skills/git.py`; edit `sera/cli/main.py` (3 new subcommands under the existing `skills` group); new `tests/test_skill_git.py` (16 tests).

## Verification

```bash
pytest -q tests/test_skill_git.py       # 16 passed
pytest -q                                # 433 passed total (was 417 + 16 new)
python -m pyflakes sera/                 # 0 warnings
```

Phase verification clause: `test_log_returns_chain_in_reverse_chronological_order` — 3 sequential edits via `commit_skill_change`, `skill_log` returns `["third edit", "second edit", "first edit"]`. The CLI E2E equivalent is `test_cli_skills_commit_then_log`.

## Dependencies

P-22.

## Notes

_Journal: decisions, blockers, commit refs go here._

**TDD vertical-slice loop (4 cycles, RED→GREEN each):**

1. RED→GREEN: `ensure_repo` lazy init + idempotent + auto-create-dir.
2. RED→GREEN: `commit_skill_change` records SKILL.md; `skill_log` returns the chain; multi-edit history; per-skill filtering; no-op commit silent; unknown-skill → `[]`; author override.
3. RED→GREEN: `skill_diff` between two refs; default `HEAD~1..HEAD`; unknown skill → empty string.
4. RED→GREEN: `sera skills commit / log / diff` CLI end-to-end + empty-history notice.

**Design decisions (2026-05-21):**

- **Wrap the `git` CLI, don't depend on GitPython.** Sera ships against `pip install sera` users who may not have GitPython. `git` CLI is ubiquitous on dev machines; absence is detected via `GitNotAvailable` and the feature degrades gracefully (the rest of Sera works without history). 60 lines of subprocess vs a 2MB library — easy call.
- **NUL-separated log format.** `--pretty=format:%H%x00%at%x00%an <%ae>%x00%s` makes the parse trivial regardless of what the user puts in a commit message. The author email lives inside the `%an <%ae>` field so a single `author` string suffices.
- **No-op commits return `None`, not raise.** Re-committing an unchanged manifest is the normal idempotent shape — auto-commit hooks fire on every save event whether or not anything changed. Silent skip + explicit `None` lets callers branch on "did anything actually land?" without parsing stderr.
- **`commit_skill_change(author=...)` over a wrapper class.** Two callers will ever exist: user-driven edit + curator-driven edit. A param is cleaner than `UserCommitter` / `CuratorCommitter` subclasses. The author *string* itself is the format `git` accepts (`"Name <email>"`).
- **`--root` lives on the group, not the subcommand.** Click subcommands access `ctx.parent.params["root"]`. Adding `--root` to each subcommand would let users write `sera skills commit foo --root X --message Y` which is technically equivalent but inconsistent with `sera skills log foo --root X`. Single source of truth.
- **`git diff --cached --quiet` not via `_run_git`.** That helper raises on non-zero, but `--quiet` returns 1 *on purpose* when changes exist. Direct `subprocess.run` here is the right escape hatch — documented in the body comment.
- **No auto-commit hook in this slice.** Wiring `VaultSync.write_chunk` or `SkillRegistry.refresh` to call `commit_skill_change` is a one-line follow-up but it's a different concern (filesystem watcher integration). P-27 ships the storage layer; the watcher integration lands in P-22.5 or as part of P-30's "snapshot" phase.
- **`.gitignore` ships with the repo.** Pre-existing per-session lockfiles under `~/.sera/locks/` shouldn't pollute history. The shipped `.gitignore` is intentionally minimal — every line earns its keep.
- **Empty history is `[]`, not exception.** `skill_log(unknown_name)` returning `[]` makes downstream "show history if non-empty" logic trivial. Raising would force every caller to wrap in try/except for a non-error case.
- **`skill_diff` with one ref defaults the other to HEAD.** Mirrors `git diff REF` semantics. Both omitted → `HEAD~1..HEAD` ("show me the latest edit") which is the dominant user query.

**What's deliberately deferred:**

- **Auto-commit on edit.** Discussed above — lands when watcher integration matures.
- **Branching / merging.** A skill is a single linear history; branching only matters if multiple authors race the same manifest. Not in scope until cross-device sync (P-90s) brings that case in.
- **Tag-on-promote.** Could tag the commit where `verify_via_replay` flipped `verified=1`. Useful but adds a state machine; defer to P-28+ if the user asks.
- **Restore-from-commit.** `sera skills restore <name> <sha>` would `git checkout <sha> -- <manifest>` + auto-commit. The plumbing is one CLI command on top of existing helpers; ship when there's a workflow that asks for it.
