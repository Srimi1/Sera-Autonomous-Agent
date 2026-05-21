# P-21 — Skills directory + manifest

## Status

done (shipped 2026-05-21, this session). TDD vertical-slice loop.

## Outclass claim

**Schema + version + lineage in manifest.** Hermes ships free-form markdown; OpenHuman removed skills runtime. Sera's manifest is structured from day one: required `name/trigger/permission/version`, optional typed `args_schema/lineage/council`. Failing validation raises at load — no silent skill-shaped objects in the registry.

## Goal

Skills are first-class.

## Deliverables

- `sera/skills/loader.py`:
  - `Skill` frozen dataclass: `name`, `trigger`, `permission`, `version`, `body`, `path`, `args_schema: dict | None`, `lineage: tuple[str, ...]`, `council: bool`.
  - `SkillManifestError(ValueError)`.
  - `load_skill(path)` — parses YAML frontmatter, validates required fields, coerces `lineage` (string → 1-tuple, list → tuple), rejects non-dict `args_schema`.
  - `discover_skills(root)` — walks `<root>/<name>/SKILL.md` and returns skills sorted by directory name. Empty / missing root → `[]`. Broken manifests propagate.
- `sera/cli/main.py` — `sera skills [--root PATH]` subcommand prints a Rich table (name, trigger, permission, version, council, lineage). Defaults to `SKILLS_DIR` (`~/.sera/skills`).
- `tests/eval_cases/skills/{caveman,council,egoist}/SKILL.md` — three hand-written skills covering the schema corners:
  - `caveman` — minimal manifest plus single lineage entry (string).
  - `egoist` — required-only plus lineage list.
  - `council` — full schema: `council: true`, `args_schema` (JSON-schema-shaped), multi-entry `lineage`.

## Files touched

new `sera/skills/__init__.py`, `sera/skills/loader.py`; edit `sera/cli/main.py`; new `tests/eval_cases/skills/{caveman,council,egoist}/SKILL.md`; new `tests/test_skills_loader.py` (18 tests).

## Verification

```bash
python -m sera.cli.main skills --root tests/eval_cases/skills   # 3 rows
pytest -q tests/test_skills_loader.py    # 18 passed
pytest -q                                 # 325 passed total (was 307 + 18 new)
python -m pyflakes sera/                  # 0 warnings
```

## Dependencies

P-04.

## Notes

_Journal: decisions, blockers, commit refs go here._

**TDD vertical-slice loop:**

1. RED→GREEN: tracer — `load_skill` returns a `Skill` with the four required fields.
2. RED→GREEN: missing-field parametrize × 4 + no-frontmatter → `SkillManifestError`.
3. RED→GREEN: optional fields default + populate + lineage-string-coercion.
4. RED→GREEN: `discover_skills` returns every conforming dir, empty root, missing root.
5. (Already-GREEN observation: skip non-conforming dirs, top-level files, propagate manifest errors.)
6. RED→GREEN: `sera skills` CLI lists discovered skills + empty notice.
7. Refactor: ship three bundled skills; lock phase verification with `test_bundled_three_skills_discoverable`.

**Design decisions (2026-05-21):**

- **Required-field validation at load.** `KeyError: 'name'` is a hostile error message. `SkillManifestError` with a list of missing fields tells the user what to fix without reading the loader source.
- **`lineage` accepts string or list.** YAML's natural way to write "one parent" is a bare string; forcing every author to use a `[ ]` array would be tax for no win. Loader coerces; downstream sees `tuple[str, ...]` always.
- **`args_schema` is loose-typed dict.** Could use Pydantic for stricter validation, but JSON-schema vocabularies vary by runtime. Keep the dict open; the consumer (skill executor, P-22+) validates against its own constraints.
- **`council: bool` rather than a structured spec.** A boolean flag is enough to tag a skill as council-eligible. The actual panel + fusion rules live in the council module (P-31+). Skills mark intent; the runtime supplies mechanism.
- **Broken manifest fails loudly.** `discover_skills` lets the `SkillManifestError` propagate rather than catching and warning. Silent skip leaves a broken skill stranded forever; loud failure forces the author to fix it.
- **CLI takes `--root` for testability.** Hard-coding `~/.sera/skills` would make the CLI smoke test depend on the host filesystem. Explicit override keeps both default convenience and test isolation.
- **Bundled three live under `tests/eval_cases/skills/`.** The phase verification requires "3 hand-written skills appear in `sera skills`". Bundling them in-repo means the verification clause holds for every checkout, not just one with a pre-populated `~/.sera`. They double as example manifests for new skill authors.
- **Skill executor is out of scope.** P-21 ships discovery + listing only. Trigger matching, arg validation, and actual execution land in P-22 (skill runtime) or wherever the next phase says. Resist the urge to wire it now — vertical-slice discipline.
