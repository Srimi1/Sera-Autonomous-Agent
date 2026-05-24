# Phase 1: Audit P1 fixes + CI gate + Epoch 6 gateway (P-51..P-53)

- **Date**: 2026-05-23
- **Slug**: audit-p1-fixes-ci-gate-epoch-6-gateway-p-51p-53

## Goal

Close audit P1s, gate future pushes, then open Epoch 6 (Channels) with P-51..P-53.

**Audit phase:**
1. Patch the 4 P1s (genesis.py blocklist gaps, skillpack path traversal)
2. Wire CI + pre-push hook so Sera holds its own commits to the P-48 standard
3. Push the 45-commit backlog (P-32..P-50 + fixes) to origin/main

**Epoch 6 phase:**
4. P-51 — Gateway server with agent-aware routing (bandit + budget + council on every event). Originally planned as foundation-only; user picked "path 2" → force outclass.
5. P-52 — Telegram adapter with 24h per-user session continuity.
6. P-53 — Discord adapter unifying slash + DM + thread into one inbox keyed by user_id.

## Changes

### Diff stat
```
(no staged/unstaged changes)
```

### Untracked files
```
(no untracked files)
```

### Recent commits
```
3a7d0d5 P-53: Discord adapter — slash + DM + thread unified into one inbox
ae467c6 P-52: Telegram adapter — 24h per-user session continuity
90b695c P-51: agent-aware gateway — webhook router runs bandit + budget + council
586ff6e ci: GitHub Actions gate (pytest + 8-pattern secret scan + pyflakes) + pre-push hook
4f973ce audit P1 fixes: AST sandbox-escape blocklist + skillpack path traversal
b281187 P-50: tool quality dashboard — per-tool usage / success / latency / $/call
61b1ffe P-49: tool-gen eval gate — auto-tools quarantined until 3 cases pass
d1f58a8 P-48: tool-gen at runtime — agent authors new tools, mypy+sandbox-gated
27390c3 P-47: plugin manifest spec — permissions declared up-front, Ed25519 signed
f696930 P-46: native scanners for top 5 — Slack, Discord, Telegram, Gmail, iMessage
```

## Verification

- [x] Changes were tested before this snapshot
- [x] `verified` below means *I actually ran it* — not just that the code looks right

| Check | Status | Notes |
|-------|--------|-------|
| Full test suite | ✅ | `pytest -q tests/` — 1102 passing locally (was 999 at start of session). Last run after P-53 commit. |
| Pre-push hook | ✅ | `scripts/pre_push.sh` ran end-to-end before each push; tests + 8-pattern scan + sensitive-file check all clean. |
| Secret scan | ✅ | Zero real secrets in tree; 10 test fixtures correctly classified by audit agent. |
| Audit P1 regressions | ✅ | 16 new tests in `test_genesis.py::TestASTSafetyExtended` and `test_skill_pack.py` covering os.system/Popen-bare/skillpack-traversal — all pass. |
| Git push to origin | ✅ | All 45 commits delivered (`a6be214` → `586ff6e`) via single-commit retries due to LibreSSL bug. `git rev-list --count origin/main..HEAD` = 0. |
| P-51 outclass: bandit+budget+council in router | ✅ | `test_gateway.py::TestEndToEnd::test_http_post_drives_bandit_update` — 3 HTTP POSTs → bandit arm.n ≥ 3. Hard-cap blocks LLM call (`llm.calls == 0`). |
| P-52 outclass: 24h continuity | ✅ | `test_telegram_adapter.py::TestE2EVerification::test_message_reply_24h_preserved` — 23h gap reuses session, 50h gap resets. |
| P-53 outclass: slash/DM/thread unified | ✅ | `test_discord_adapter.py::TestRouterIntegration::test_three_surfaces_one_session_e2e` — 3 dispatches across surfaces → 1 active session. |
| CI workflow YAML | ⚠️ | YAML parses locally (`yaml.safe_load`) but I have NOT seen GitHub Actions actually run it yet — first PR/push will be the real test. |

## Limits

> ⚠️ Honesty rule: call out blind spots, not just successes.

**What was NOT tested:**
- **GitHub Actions workflow.** I validated the YAML parses locally but never watched a real run. First push to a PR or direct push to main will reveal: (a) whether Ubuntu's grep + bash handles the 8 secret patterns the same way macOS bash did locally, (b) whether `pip install -e ".[dev]"` resolves cleanly on a clean CI runner, (c) whether pyflakes finds issues that local doesn't.
- **Real Telegram / Discord API calls.** All sender tests use `_poster` injection. The actual `https://api.telegram.org` and `https://discord.com/api/v10` round-trips have never been exercised by Sera. Auth header format, rate limits, retry semantics, error response shape — all assumed from docs, not measured.
- **Discord interactions signature verification.** Production Discord requires Ed25519 signature checks on the Interactions Endpoint URL. P-53 ships the parser + sender but NOT the signature-verification middleware. A real Discord app will fail until P-53.5 or P-54 adds it.
- **Concurrent dispatch.** Router tests are sequential. No test for two HTTP POSTs landing in the queue simultaneously while Router.serve drains. The asyncio.Queue is single-consumer-safe by design; multi-consumer not exercised.
- **Audit P2/P3 backlog.** 35 P2 warnings and 54 P3 nits from the audit are untouched. Notably: 9 source files have no tests (all 7 tools/impl/*, 2 LLM adapters, config.py). The drift dashboard P-50 watches tools it can't unit-test.
- **macOS chat.db schema variants.** P-46 iMessage scanner builds a fixture chat.db; real macOS Big Sur+ may have evolved columns we don't query.
- **Session continuity across process restarts.** Both Telegram and Discord stores are SQLite-backed so they SHOULD survive, but no test runs `Session.load(stored_id)` after a simulated restart.
- **Gateway under load.** No stress test of the ThreadingHTTPServer; the LibreSSL bug suggests we're working with system tooling, not custom transport.

**What could still break:**
- `LibreSSL/3.3.6` push failure recurs on every multi-commit push from this Mac. Workaround in place (single-commit pushes) but each large work session pays a tax. Brew install of OpenSSL-backed git would fix it permanently.
- `default_parser` in `gateway/server.py` is liberal — non-Sera-controlled webhook callers can shape ambiguous payloads. The platform-specific parsers (P-52/53) bypass it correctly, but if someone POSTs `/webhook/unknown` we'll happily accept any JSON with a `text` field.
- `Router._session_resolver` runs synchronously inside the async dispatch. A slow SQLite lookup blocks the event loop. SQLite is fast enough today; if the sessions DB grows large or has lock contention it'll show.
- `DiscordSender.reply_hook` falls back to channel-message when a slash event is missing `interaction_token`. Falling back from interaction → channel may silently drop the user's ephemeral expectation.

**Dependencies / assumptions:**
- Stdlib-only on the network paths (urllib via `asyncio.to_thread`). Acceptable for now; if we hit perf or HTTP/2 needs we switch to httpx (already a dep).
- `ThompsonBandit.update` is called with `cost_usd=0.0` from the router — the per-turn cost lives inside `run_turn` and isn't surfaced. Bandit reward gates only on success + latency right now. P-54 or later should plumb actual cost out of `run_turn`.
- Telegram/Discord token format assumed: token never empty (P-52/P-53 enforce); `Bot <token>` prefix for Discord; raw token for Telegram. Real tokens not tested.
- `Session.create()` creates a new DB row every time — no guarantee that 10 000 abandoned sessions/day don't bloat the sessions DB. No cleanup job yet.

## Follow-ups

**Immediate (this week):**
- [ ] **Watch the first CI run.** Push a no-op PR or commit and confirm `.github/workflows/test.yml` (pytest, secret-scan, pyflakes) all pass green on GitHub.
- [ ] **Brew install git** to bypass the LibreSSL push bug — single-commit pushes were the only way to deliver the 45-commit backlog this session.
- [ ] **Backfill the 9 untested files** (audit recommendation #4):
  - `sera/tools/impl/{browser, file_read, file_write, memory_store, python_eval, shell_run, web_search}.py`
  - `sera/llm/adapters/{openai, anthropic}_adapter.py` (mock-driven streaming tests like MCP client)
  - `sera/config.py` (DEFAULT_CONFIG round-trip)
- [ ] **Fix `sera/safety/approval.py:11` layering violation** — safety should not import `sera.tools.base`. Move `ToolCall` to a neutral location.

**Next phase (P-54..P-55):**
- [ ] P-54 — Slack adapter (interactive blocks for approvals). Build on the Discord pattern: parse multiple Slack surfaces (slash, DM, mention), Block Kit payloads for approval modals.
- [ ] P-55 — WhatsApp via desktop bridge (NOT Cloud API). Privacy-first outclass.

**Epoch 6 wrap (P-56..P-65):** orchestration + scheduling, voice (offline-first per [[project-sera]] decisions).

**Tech debt for the next session:**
- Plumb actual `_turn_cost` from `run_turn` back to the router so bandit reward signal gates on cost too, not just latency.
- Discord Interactions Endpoint Ed25519 signature verification middleware (production blocker if shipping the Discord bot).
- Session cleanup job — long-tail SQLite growth in `sessions.db` from one-off webhook events.

**Memory notes for future sessions:**
- The audit-out/ directory is full of analysis artifacts; safe to delete or keep as historical reference.
- `scripts/install_hooks.sh` is idempotent; re-run any time you re-clone.
- Pre-push hook is symlinked, not copied — `scripts/pre_push.sh` edits take effect immediately on next push.

---

*Auto-generated by save-phase skill. Do not edit the header manually.*
