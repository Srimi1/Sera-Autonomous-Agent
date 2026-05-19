# P-13 — Embedder + multi-modal

## Status

pending.

## Outclass claim

**Image + text in the same vector space** via vision-caption-then-embed. OH is text-only; H does vision via tools.

## Goal

One vector per chunk regardless of modality.

## Deliverables

- `sera/memory/embedder.py` — OpenAI `text-embedding-3-small` (1536). Image path: vision model captions image → caption prefixed with `[image]` → embedded.

## Files touched

`sera/memory/embedder.py`.

## Verification

```bash
  pytest -q tests/test_embedder.py
  # bench: image query and matching text query retrieve same chunk
  ```

## Dependencies

P-11.


## Notes

_Journal: decisions, blockers, commit refs go here._
