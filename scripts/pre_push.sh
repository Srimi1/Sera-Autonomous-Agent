#!/bin/bash
# scripts/pre_push.sh — Sera pre-push hygiene gate
# Runs pytest + 8-pattern secret scan + sensitive-file check before push.
# Install: ln -s ../../scripts/pre_push.sh .git/hooks/pre-push
#          (or `bash scripts/install_hooks.sh`)
#
# Bypass for emergencies: git push --no-verify
set -e

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

# ─── 1. tests ────────────────────────────────────────────────────────────────
echo "==> [1/3] running tests..."
if [ -d tests ]; then
  if command -v python3.11 >/dev/null 2>&1; then
    PY=python3.11
  else
    PY=python3
  fi
  $PY -m pytest -q tests/ || { echo "FAIL: tests failed, push blocked"; exit 1; }
else
  echo "SKIP: no tests/ dir found"
fi

# ─── 2. 8-pattern secret scan ────────────────────────────────────────────────
echo "==> [2/3] secret scan..."
PATTERNS=(
  # 1. OpenAI keys
  'sk-[A-Za-z0-9]{20,}'
  # 2. Anthropic keys
  'sk-ant-[A-Za-z0-9_-]{20,}'
  # 3. AWS access keys
  'AKIA[0-9A-Z]{16}'
  # 4. Google / GCP API keys
  'AIza[0-9A-Za-z_-]{35}'
  # 5. GitHub PATs (classic + fine-grained)
  'gh[pousr]_[A-Za-z0-9]{36,}'
  # 6. Slack tokens
  'xox[abprs]-[A-Za-z0-9-]{10,}'
  # 7. PEM private key blocks
  '-----BEGIN (RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY'
  # 8. Generic high-entropy assignments
  '(api[_-]?key|secret|token|password)[[:space:]]*[:=][[:space:]]*"[A-Za-z0-9_/+=-]{20,}"'
)

FAIL=0
for pat in "${PATTERNS[@]}"; do
  hits=$(git ls-files | xargs grep -EnI --exclude='*.env.example' "$pat" 2>/dev/null || true)
  # Filter out obvious test fixtures
  real_hits=$(echo "$hits" | grep -v -E '(tests/|test_|fake|dummy|stub|FAKE|DUMMY|STUB|AAAA+|0000+)' || true)
  if [ -n "$real_hits" ]; then
    echo "  ✗ SECRET MATCH (pattern: $pat):"
    echo "$real_hits" | head -5 | sed 's/^/    /'
    FAIL=1
  fi
done

if [ $FAIL -eq 1 ]; then
  echo "FAIL: secret-scan tripped, push blocked. Investigate hits above."
  echo "      Bypass (emergencies only): git push --no-verify"
  exit 1
fi

# ─── 3. sensitive filenames ──────────────────────────────────────────────────
echo "==> [3/3] sensitive-file check..."
bad=$(git ls-files | grep -E '(^|/)\.env($|\.)|\.(key|pem|p12|pfx)$' | grep -v '\.env\.example' || true)
if [ -n "$bad" ]; then
  echo "FAIL: sensitive files tracked:"
  echo "$bad" | sed 's/^/  /'
  exit 1
fi

echo "ok to push"
