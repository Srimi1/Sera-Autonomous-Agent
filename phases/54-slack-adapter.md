# P-54 — Slack adapter

## Status

done.

## Outclass claim

**Interactive blocks for approvals** in-channel.

## Files

`sera/gateway/platforms/slack.py`.

## Verification

approval block surfaces from a workspace.

## Dependencies

P-51.


## Notes

2026-05-24: `sera/gateway/platforms/slack.py` — `parse_slack()` unifies 4 inbound shapes: slash command (pre-decoded form dict), event_callback channel/dm/app_mention, block_actions interactive callbacks. `SlackSender`: `send_message()` plain text, `send_approval_block()` posts Block Kit with Approve (primary) + Reject (danger) buttons, `ack_block_action()` responds to response_url within 3-second window replacing the buttons. `reply_hook()` routes block_action to ack_block_action, others to send_message. `_approval_blocks()` builds the Block Kit JSON. `SlackSessionStore`: SQLite 24h per-user session continuity across all surfaces including block_action — approval clicks share session with the originating slash command. 42 tests, 1144 total.
