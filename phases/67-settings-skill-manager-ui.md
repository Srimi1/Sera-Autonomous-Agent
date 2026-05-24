# P-67 — Settings + skill manager UI

## Status

done (backend verified; React not run).

## Outclass claim

Skill manager view-model surfaces lifecycle **state** + **quality score** per
skill (from P-24 lifecycle + P-29 scoring), and the settings view-model
**redacts secret-shaped config values** before they ever reach the panel — the
UI can never receive a raw API key. Most settings UIs happily render secrets in
the clear.

## Files

- `sera/shell/viewmodels.py::skills_overview`, `settings_overview`
- `sera-shell/src/components/{Settings,Skills}.tsx` — consumers (not run here)
- `tests/test_shell_viewmodels.py::TestSkillsOverview`, `TestSettingsOverview`

## Verification

| Check | Status | Notes |
|-------|--------|-------|
| skills_overview tested | ✅ | lists skill, state, enabled, optional score |
| settings redaction | ✅ | api_key / auth_token → "••••••"; non-secrets pass through |
| full suite | ✅ | no regressions |

## Limits

- **"Enable a skill from UI → A/B kicks in" (literal verification) NOT wired
  end-to-end.** The skills view-model is read-only here; the PATCH
  enable/disable endpoint and its hook into the P-26 A/B harness are not yet
  wired. Skills.tsx posts to `PATCH /v1/skills/<name>` which doesn't exist yet.
- **Settings/Skills.tsx not executed**; `GET /v1/settings`, `/v1/skills` not
  wired.

## Dependencies

P-26, P-61.
