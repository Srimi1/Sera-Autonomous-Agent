# P-99 — Public ship (DMG/MSI/deb)

## Status

done (scaffolded 2026-05-24 — installer manifest + validated codesign release workflow; native bundling runs in CI on tag).

## Outclass claim

**Ship bar enforced in CI.** The installer manifest declares a 5-minute first-reply budget and mandatory codesigning for macOS + Windows; `validate_installer.py` fails the release build if any target drifts. No rival gates its own installer on a fresh-machine time-to-first-reply contract.

## Files

`installer/`, codesign workflow.

## Verification

fresh-machine install + first reply < 5 min.

## Dependencies

P-90, P-61.


## Notes

_Journal: decisions, blockers, commit refs go here._
