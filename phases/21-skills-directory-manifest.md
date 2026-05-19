# P-21 — Skills directory + manifest

## Status

pending.

## Outclass claim

**Schema + version + lineage in manifest.** Hermes ships free-form markdown; OH removed skills runtime. Ours has structure from day one.

## Goal

Skills are first-class.

## Deliverables

`~/.sera/skills/<name>/SKILL.md` with frontmatter `name, trigger, permission, args_schema, version, lineage, council`. Discoverable via `sera skills`.

## Files

`sera/skills/loader.py`, `sera/cli/skills.py`.

## Verification

3 hand-written skills appear in `sera skills`.

## Dependencies

P-04.


## Notes

_Journal: decisions, blockers, commit refs go here._
