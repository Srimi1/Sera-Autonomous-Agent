# P-22 — Skill loader + dynamic tool registration

## Status

done (shipped 2026-05-21, this session). TDD vertical-slice loop.

## Outclass claim

**Hot reload** — edit a skill, next `refresh()` registers / re-registers / drops without restart. Per-root singleton tracks mtimes; second reload with no on-disk changes is a no-op the CLI prints as "no changes." Rivals require a process restart to pick up skill edits.

## Goal

Skills become tools at runtime.

## Deliverables

- `sera/skills/loader.py`:
  - `skill_to_tool(skill)` — adapts a `Skill` manifest to a `Tool` registry entry. Tool name is namespaced `skill.<name>`, scope=SKILL, description = first paragraph of body, parameters = `args_schema` if present else `{"type": "object", "properties": {}}`. Handler returns the skill body (skeleton — real executor wired later).
  - `SkillRegistry(root)` — owns `skill_name → (tool_name, mtime)` map.
    - `refresh()` rescans root, returns `RefreshSummary(added, removed, updated, changed)`. New manifests → register. Disappeared dirs → unregister. mtime-changed → re-register.
    - `tools()` snapshot of currently-registered skill tools.
    - `clear()` unregister-all (test helper / clean teardown).
  - `get_default_registry(root)` — process-wide singleton keyed by resolved root path; lets repeated invocations see real deltas.
  - `reset_default_registries()` — test hatch.
- `sera/tools/registry.py` — `unregister(name)` helper for one-shot drop without touching `_discovered`.
- `sera/cli/main.py` — `sera skills [--reload]` flag:
  - Without `--reload`: read-only listing (P-21 behaviour preserved).
  - With `--reload`: invokes the singleton registry's `refresh()`, prints `+N added / -N removed / ~N updated` delta, names per group. "No changes" notice when delta is empty.

## Files touched

`sera/skills/loader.py`, `sera/tools/registry.py`, `sera/cli/main.py`; `tests/test_skills_loader.py` (P-21 file extended with 11 new tests). Bundled `tests/eval_cases/skills/{caveman,egoist}/SKILL.md` updated to use `READ_ONLY` (matches `Permission` enum vocabulary).

## Verification

```bash
pytest -q tests/test_skills_loader.py    # 29 passed
pytest -q                                 # 336 passed total (was 325 + 11 new)
python -m pyflakes sera/                  # 0 warnings

# Manual hot reload smoke:
python -m sera.cli.main skills --root tests/eval_cases/skills --reload
# → "Reload summary: +3 added, -0 removed, ~0 updated"
python -m sera.cli.main skills --root tests/eval_cases/skills --reload
# → "no changes since last reload"
# (then edit caveman/SKILL.md body and re-run — reports `~1 updated`.)
```

## Dependencies

P-21.

## Notes

_Journal: decisions, blockers, commit refs go here._

**TDD vertical-slice loop (5 cycles, RED→GREEN each):**

1. RED→GREEN: tracer — `skill_to_tool` returns a Tool with correct name (`skill.<name>`), permission, scope, body-derived description.
2. RED→GREEN: handler returns body; default `parameters` is open-object; `args_schema` flows through when set.
3. RED→GREEN: `SkillRegistry.refresh()` registers every discovered skill; write-through to global tool registry; `clear()` cleans up.
4. RED→GREEN: refresh delta — added / removed / updated correctly populated. Already-passing on `refresh` implementation, tests lock the contract.
5. RED→GREEN: `sera skills --reload` invokes singleton, prints delta. Second invocation sees no changes (forces the singleton design).

**Design decisions (2026-05-21):**

- **`skill.<name>` namespace.** Skill-derived tools live under a dotted prefix so they can never collide with a system tool of the same short name. The Tool registry is one flat dict; namespacing at the name level is the cheapest disambiguation.
- **Open-object default for `args_schema`.** A skill author who didn't supply a schema still needs to be callable by an LLM that wants to invoke them. `{"type": "object", "properties": {}}` is the minimal schema OpenAI / Anthropic accept without rejecting the function definition.
- **Description = first paragraph, not whole body.** The body can run pages. A first-paragraph slice is what the LLM sees during tool-selection — that's where the trigger/intent description belongs by convention. Full body is still available via `tool.handler(...)` / `skill.body`.
- **Skeleton handler returns the body.** P-22 ships the *registration* mechanism, not the runtime semantics. Real skill execution (LLM-prompted, council-fanned, args-validated) lands in a later phase. The handler-returns-body contract keeps every other test useful without forcing premature design.
- **mtime as the change signal.** Content hashing would be more accurate but ~100× slower for large bodies. mtime + size is what watchdog / fswatch backends use anyway. Tests explicitly bump mtime via `os.utime` rather than racing the filesystem.
- **Per-root singleton (`get_default_registry`).** Without persistent state across CLI invocations, every `--reload` would see every skill as "added" forever. A singleton keyed by resolved root path makes second-invocation deltas honest and matches user mental model ("hot reload" implies process-level state).
- **`unregister` over `_registry.pop`.** Added a public surface on `sera.tools.registry` so callers don't reach into the underscore-prefixed dict. Returns `bool` so the caller can detect a no-op cleanly.
- **`READ_ONLY` not `READ`.** The bundled SKILL.md files used `READ` (matches the conceptual mode in some rivals) but `Permission` enum's vocabulary is `READ_ONLY`. Fixed at the SKILL.md surface rather than aliasing in the enum — keeps the permission vocabulary one place.
- **No `watchdog` hook yet.** Hot reload happens on explicit `refresh()` (CLI flag or programmatic). Plumbing a watcher into the agent loop's per-turn pre-pass is a P-22.5+ task — same mtime detection, just driven by a thread instead of a user. Out of scope for the skeleton.
- **CliRunner-friendly singleton.** The default registries cache survives pytest test functions in the same process. `reset_default_registries()` exists for tests that need a clean slate; production callers never need it.
