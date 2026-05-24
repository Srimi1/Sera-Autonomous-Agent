# P-75 — Adversarial self-play

## Status

done.

## Outclass claim

**Red vs blue agents patching skills and memory.** Nobody ships an agent
that attacks its own context to harden the system.  5 canonical injection
payloads (IGNORE / ROLE_SWITCH / EXFIL / OVERRIDE / NESTED) planted by the
red agent are caught by the blue agent every time.  P-81 (semantic classifier)
plugs into the same seam when it ships.

## Files

- `sera/redteam/__init__.py`, `sera/redteam/red.py`, `sera/redteam/blue.py`
- `tests/test_redteam.py` — 21 tests

## Verification

| Check | Status |
|-------|--------|
| 21 tests | ✅ |
| planted injection caught | ✅ |
| all 5 payload ids caught | ✅ |
| injectable classifier (P-81 seam) | ✅ |
| tool_result / skill_body / memory_chunk plant | ✅ |

## Dependencies

P-30. (P-81 upgrades blue's classifier — not a hard dependency.)
