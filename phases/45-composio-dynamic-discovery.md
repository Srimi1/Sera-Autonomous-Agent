# P-45 — Composio dynamic discovery

## Status

done.

## Outclass claim

**Runtime action manifest.** OH hardcodes Composio actions.

## Goal

Connect Gmail → actions become tools immediately.

## Files

`sera/integrations/composio.py`.

## Verification

connect Gmail OAuth → `composio__gmail__send_email` appears in `sera tools` without restart.

## Dependencies

P-22.


## Notes

2026-05-23: `sera/integrations/composio.py` — ComposioDiscovery with injectable _client for testing. action_to_tool_name(GMAIL_SEND_EMAIL)→composio__gmail__send_email (replace first _ with __). composio_action_to_tool() handles both flat dict and OpenAI-format (function.name) schemas. refresh(apps) fetches actions, registers as Sera Tools with EXECUTE permission. unregister_all() removes them. Verification: before refresh → tool absent; after refresh(["GMAIL"]) → composio__gmail__send_email in all_tools() without restart. Graceful degradation when composio-openai not installed. 25 tests, 873 total.
