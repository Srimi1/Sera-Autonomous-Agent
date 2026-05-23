# P-56 — Email (IMAP+SMTP)

## Status

done.

## Outclass claim

**Threaded replies** with subject preserved + In-Reply-To.

## Files

`sera/gateway/platforms/email.py`.

## Verification

reply lands in thread.

## Dependencies

P-51.


## Notes

2026-05-24: `sera/gateway/platforms/email.py` — `parse_email()` parses raw RFC822 (bytes or str) via stdlib email+policy.default: From address → user_id, body via get_body(preferencelist plain→html), HTML stripped if no plain part. Threading headers (Message-ID, In-Reply-To, References) carried in metadata. `re_subject()` idempotent Re: prefix (no "Re: Re:" stacking, case-insensitive). `build_references()` extends parent References + parent Message-ID per RFC 5322 §3.6.4, dedups. `EmailSender.build_reply()` builds EmailMessage with Re: subject + In-Reply-To + References + fresh Message-ID → reply lands in thread. SMTP send via injectable _transport (real path: STARTTLS + login + send_message). `EmailPoller.poll_unseen()` IMAP SEARCH UNSEEN → FETCH RFC822 → parse → mark \\Seen; injectable _client_factory for tests. `EmailSessionStore`: 7-day TTL (threads outlive chat) per-sender-address. E2E test verifies reply threads correctly. 42 tests, 1220 total. pyflakes clean.
