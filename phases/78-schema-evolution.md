# P-78 — Schema evolution

## Status

done.

## Files

`sera/memory/migrations/__init__.py`, `sera/memory/migrations/runner.py`, `tests/test_migrations.py` — 13 tests.

## Verification

P-11 snapshot (v1 schema + data) migrated to v4 (current) without data loss (test_v1_snapshot_migrates_to_current). 4 versioned migrations; idempotent re-run; partial target.

## Dependencies

P-11.


## Notes

_Journal: decisions, blockers, commit refs go here._
