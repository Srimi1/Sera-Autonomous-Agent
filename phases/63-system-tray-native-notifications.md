# P-63 — System tray + native notifications

## Status

done (scaffolded 2026-05-24).

## Outclass claim

**Loop-event notifications.** Rivals notify on user messages. Sera notifies on its own internal events — memory consolidation complete, LoRA gain recorded, injection attempt blocked. Injectable runner; zero call-site changes at verifying the binary exists.

## Files

`sera-shell/src-tauri/src/tray.rs`.

## Verification

tray works macOS + Windows; notifications visible.

## Dependencies

P-61.


## Notes

_Journal: decisions, blockers, commit refs go here._
