# P-66 — Accounts panel

## Status

done (backend verified; OAuth round-trip + React not run).

## Outclass claim

Foundation panel. Testable value: a view-model that groups the live Composio
tool registry by app, so the panel shows one row per connected service with its
tool count — derived from the actual registered tools, not a hardcoded list.

## Files

- `sera/shell/viewmodels.py::accounts_overview` — tested backend
- `sera-shell/src/components/Accounts.tsx` — consumer (not run here)
- `tests/test_shell_viewmodels.py::TestAccountsOverview`

## Verification

| Check | Status | Notes |
|-------|--------|-------|
| accounts_overview tested | ✅ | 4 tests |
| groups tools by app | ✅ | github×2 + gmail×1 → correct rows |
| degrades on client error | ✅ | broken client → empty, no crash |
| full suite | ✅ | no regressions |

## Limits

- **Gmail OAuth round-trip (the phase's literal verification) NOT performed** —
  requires real Composio credentials + a browser; impossible here. The
  account-listing logic that the panel renders post-OAuth is tested; the OAuth
  flow itself is deferred to a credentialed machine.
- **Accounts.tsx not executed**; `GET /v1/accounts` endpoint not yet wired.

## Dependencies

P-45, P-61.
