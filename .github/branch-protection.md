# Branch Protection — `main`

Configure these rules in GitHub → Settings → Branches → Add rule for `main`:

## Required status checks (must pass before merge)

| Check name | Source |
|---|---|
| `eval / golden` | `eval.yml` matrix cell |
| `eval / jailbreak` | `eval.yml` matrix cell |
| `pytest (Python 3.11)` | `test.yml` |
| `secret scan (8 patterns)` | `test.yml` |

All four must be green. No bypass. No admin exemption.

## Settings

- [x] Require status checks to pass before merging
- [x] Require branches to be up to date before merging
- [x] Do not allow bypassing the above settings

## Enforcement via `gh` CLI

```bash
gh api repos/{owner}/{repo}/branches/main/protection \
  --method PUT \
  --field required_status_checks='{"strict":true,"contexts":["eval / golden","eval / jailbreak","pytest (Python 3.11)","secret scan (8 patterns)"]}' \
  --field enforce_admins=true \
  --field required_pull_request_reviews=null \
  --field restrictions=null
```

Replace `{owner}/{repo}` with the actual repository path.
