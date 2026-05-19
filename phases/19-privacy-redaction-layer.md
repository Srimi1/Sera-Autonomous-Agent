# P-19 — Privacy + redaction layer

## Status

pending.

## Outclass claim

**Search-with-consent.** PII tagged at ingest; searchable only after explicit consent toggle per query.

## Goal

Sera never silently surfaces SSN, card number, secret tokens.

## Deliverables

- `sera/memory/privacy.py` — PII detector (regex + Microsoft Presidio if installed).
  - Tagged chunks return "[redacted match — confirm to reveal]" by default.

## Files touched

`sera/memory/privacy.py`, `sera/memory/search.py`.

## Verification

```bash
  pytest -q tests/test_privacy.py
  ```

## Dependencies

P-11.


## Notes

_Journal: decisions, blockers, commit refs go here._
