# P-14 — Obsidian vault sync (bidirectional)

## Status

pending.

## Outclass claim

**Two-way sync.** User edits a vault `.md` → file watcher → re-ingest. OH mirrors one-way.

## Goal

Memory is editable by hand.

## Deliverables

- `sera/memory/vault.py` — write `~/.sera/vault/<source>/<chunk-id>.md` with YAML frontmatter. Watchdog observer for changes; debounced re-ingest.

## Files touched

`sera/memory/vault.py`.

## Verification

```bash
  pytest -q tests/test_vault_sync.py
  # manual: edit a vault file, search reflects under 2s
  ```

## Dependencies

P-11, P-12.


## Notes

_Journal: decisions, blockers, commit refs go here._
