# P-14 — Obsidian vault sync (bidirectional)

## Status

done (shipped 2026-05-21, this session).

## Outclass claim

**Two-way sync.** User edits a vault `.md` → file watcher → re-ingest. OH mirrors one-way.

## Goal

Memory is editable by hand.

## Deliverables

- `sera/memory/tree.py` — `update_chunk(id, content?, summary?, confidence?, embedding?)` (vss row replaced when embedding changes; UPDATE elsewhere) and `delete_chunk(id)` (drops chunks + chunks_vss row).
- `sera/memory/vault.py`:
  - `split_frontmatter(text) / render_file(meta, body)` — YAML frontmatter codec used by every read/write.
  - `VaultSync(tree, vault_dir, embedder=None)` —
    - `path_for(source, chunk_id)` resolves under `vault_dir/<safe-source>/<id>.md`, sanitizing the source segment so `../etc/passwd` etc. stay inside the vault.
    - `write_chunk(id)` renders one chunk; `export_all()` bootstraps the vault from an existing DB.
    - `ingest_file(path)` upserts by frontmatter `id`: present + alive → update; present + dead → insert + rewrite file with the canonical id; absent → insert + rewrite. Returns `IngestResult` with `action ∈ {inserted, updated}`.
    - `sync_from_disk()` walks the vault and re-ingests every `*.md`.
    - When an `Embedder` is configured, every ingest re-embeds the body and the new vector lands in the chunk row.
  - `VaultWatcher(vault_dir, on_change, poll_interval=1.0)` — daemon-thread mtime poller with **two-poll stability debounce**. Fires the callback exactly once per stabilized change; partial editor writes (mtime still bouncing) don't fire. `start()` / `stop()` are idempotent.

## Files touched

new `sera/memory/vault.py`; edit `sera/memory/tree.py`; new `tests/test_vault_sync.py` (17 tests).

## Verification

```bash
pytest -q tests/test_vault_sync.py        # 17 passed
pytest -q                                  # 185 passed total (was 168 + 17 new)
python -m pyflakes sera/                   # 0 warnings
# manual: edit a vault file; with poll_interval=1.0s the search reflects within ~2s.
```

## Dependencies

P-11, P-12.

## Notes

_Journal: decisions, blockers, commit refs go here._

**Design decisions (2026-05-21):**

- **mtime-poll watcher, no `watchdog` dep.** Watchdog drags in inotify / fsevents / kqueue backends per OS, plus its own thread pool. For the skeleton, a 1s poll is fine: the verification target is "≤2s" and the debounce takes one extra poll. We'll swap in inotify if a real corpus needs sub-second turnaround.
- **2-poll stability debounce.** A single mtime change can land mid-write (Obsidian writes a temp file, then renames; some editors do multi-step saves). Requiring two consecutive polls with the same mtime means we only fire when the editor is done.
- **Frontmatter `id` is the join key.** Sqlite-assigned ids are stable for the chunk's lifetime; the file carries that id forward across edits. If the user copies a `.md` to a new path keeping the id, ingest still updates the *same* DB row — by design (lets users reorganize the vault freely without forking chunks).
- **Stale id → re-insert.** A dropped DB but kept vault directory shouldn't error or silently re-use ids; we insert a new chunk and rewrite the file with the new id so the vault becomes the source of truth again.
- **`update_chunk` is partial.** Each field is opt-in (`None` = leave alone). Callers updating only the body don't need to re-pass confidence. The vss row gets replaced wholesale when a new embedding arrives because `vss0` lacks UPDATE — that's the only path that touches the index, so embeddings stay consistent.
- **Source sanitization is mandatory.** `_safe_segment` strips path separators and other unsafe chars so `source="../etc/passwd"` lands at `vault/etc-passwd/<id>.md` inside the vault, not above it. Test covers it.
- **Watcher callback exceptions are logged + swallowed.** A bad on_change shouldn't kill the watcher thread. The exception goes to the module logger so it shows up in CLI debug output without crashing the daemon.
- **No deletion path yet.** A vault file disappearing does not delete the DB chunk (skeleton scope). Adding "purge missing on sync" is one query but interacts with user intent — a missed sync shouldn't wipe memory. Defer until we have a clearer signal (e.g. an `archived=true` frontmatter flag).
- **Async ingest, sync watcher.** `ingest_file` is async because the embedder is async. The watcher thread doesn't run an event loop itself; the CLI integration (later) will hand the path off to the main loop's pending-work queue. For now, tests drive ingest via `asyncio.run`.
