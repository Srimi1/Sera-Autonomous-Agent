#!/usr/bin/env bash
# Pre-push security guard for Sera. Blocks secrets, runtime data, and PII.
# Usage:  bash scripts/pre-push-check.sh          # scan staged + all tracked
#         bash scripts/pre-push-check.sh --staged # staged only (fast)
# Wire as a hook:  ln -sf ../../scripts/pre-push-check.sh .git/hooks/pre-push
set -uo pipefail

RED=$'\033[31m'; GRN=$'\033[32m'; YEL=$'\033[33m'; RST=$'\033[0m'
fail=0
EXCL=":(exclude).venv/**"

echo "== Sera pre-push security scan =="

# 1. Block forbidden file types from being staged/committed.
echo "-- forbidden data/secret files --"
BAD_FILES=$(git diff --cached --name-only 2>/dev/null | grep -iE '\.(env|key|pem|p12|pfx|rvf|db|sqlite|sqlite3|token)($|\.)' || true)
if [ -n "$BAD_FILES" ]; then
  echo "${RED}BLOCK${RST}: secret/data files staged:"; echo "$BAD_FILES" | sed 's/^/  /'
  fail=1
else
  echo "${GRN}ok${RST}: no .env/.key/.pem/.rvf/.db staged"
fi

# 2. Credential & key patterns in tracked content.
echo "-- credential patterns --"
CRED=$(git grep -nIE "sk-[a-zA-Z0-9]{20,}|sk-ant-[a-zA-Z0-9_-]{20,}|ghp_[a-zA-Z0-9]{30,}|github_pat_[a-zA-Z0-9_]{30,}|AKIA[0-9A-Z]{16}|xox[bpoa]-[0-9a-zA-Z-]{20,}|AIza[0-9A-Za-z_-]{35}|hf_[a-zA-Z0-9]{30,}|-----BEGIN (RSA |OPENSSH |EC |DSA |PGP )?PRIVATE KEY-----" -- . "$EXCL" 2>/dev/null \
  | grep -vE "redact\.py|injection\.py|pack\.py|/test_|tests/" || true)
if [ -n "$CRED" ]; then
  echo "${RED}BLOCK${RST}: credential-like strings:"; echo "$CRED" | sed 's/^/  /'
  fail=1
else
  echo "${GRN}ok${RST}: no live API keys / private keys"
fi

# 3. Real PII: absolute home paths and the operator's identity.
# Exceptions: this script (contains the patterns) and the two community-health
# files that intentionally carry a public reporting contact chosen by the owner.
echo "-- personal info --"
PII=$(git grep -nIE "/Users/[a-zA-Z0-9_.-]+|/home/[a-zA-Z0-9_.-]+|srijan|srimi" -- . "$EXCL" \
  ":(exclude)scripts/pre-push-check.sh" \
  ":(exclude)CODE_OF_CONDUCT.md" \
  ":(exclude)SECURITY.md" 2>/dev/null || true)
if [ -n "$PII" ]; then
  echo "${YEL}REVIEW${RST}: home paths / operator name in tracked files:"; echo "$PII" | sed 's/^/  /'
  fail=1
else
  echo "${GRN}ok${RST}: no home paths or operator identity"
fi

# 4. Embedded token in the push remote URL.
echo "-- remote url --"
if git config --get remote.origin.url 2>/dev/null | grep -qE "x-access-token|://[^/]+:[^/]+@"; then
  echo "${RED}BLOCK${RST}: credentials embedded in remote URL — use a credential helper"
  fail=1
else
  echo "${GRN}ok${RST}: remote URL carries no token"
fi

echo "================================="
if [ "$fail" -ne 0 ]; then
  echo "${RED}VERDICT: DO NOT PUSH — resolve findings above.${RST}"
  exit 1
fi
echo "${GRN}VERDICT: SAFE TO PUSH${RST}"
