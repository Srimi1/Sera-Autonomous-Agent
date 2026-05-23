#!/bin/bash
# scripts/install_hooks.sh — wire scripts/pre_push.sh into .git/hooks/pre-push
# Idempotent: re-running is safe.
set -e

REPO_ROOT="$(git rev-parse --show-toplevel)"
HOOK="$REPO_ROOT/.git/hooks/pre-push"
SCRIPT="$REPO_ROOT/scripts/pre_push.sh"

if [ ! -f "$SCRIPT" ]; then
  echo "FAIL: $SCRIPT missing"
  exit 1
fi

chmod +x "$SCRIPT"

# Install as a symlink so updates to scripts/pre_push.sh apply automatically.
if [ -L "$HOOK" ] || [ -f "$HOOK" ]; then
  rm "$HOOK"
fi
ln -s "../../scripts/pre_push.sh" "$HOOK"
echo "installed: $HOOK -> ../../scripts/pre_push.sh"
echo "next push will run: pytest + 8-pattern secret scan + sensitive-file check"
echo "bypass (emergencies only): git push --no-verify"
