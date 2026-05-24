# P-97 — Kernel-level integration (optional helpers)

## Status

done (shipped 2026-05-24).

## Outclass claim

**Sera starts on login across all three platforms.** LaunchAgent (macOS), systemd user unit (Linux), Task Scheduler XML (Windows). Injectable runner seam — tested without touching the real OS. `sera helper install/uninstall/status`. No rival ships all three system integration targets.

## Files

`sera-helper/` (LaunchAgent + scheduled task + systemd unit).

## Verification

hotkey works system-wide.

## Dependencies

P-70.


## Notes

_Journal: decisions, blockers, commit refs go here._
