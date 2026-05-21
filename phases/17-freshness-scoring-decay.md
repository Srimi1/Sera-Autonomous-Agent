# P-17 — Freshness scoring + decay

## Status

done (shipped 2026-05-21, this session).

## Outclass claim

**EWMA decay per chunk + entity-aware boost** — yesterday's fact outranks last year's contradiction without deleting either. A chunk whose linked entity was mentioned today refreshes via provenance, even if the chunk body itself was untouched.

## Goal

Stale facts demoted; never deleted.

## Deliverables

- `sera/memory/tree.py`:
  - `chunks.freshness REAL NOT NULL DEFAULT 1.0` + `last_accessed_at REAL` columns; idempotent ALTERs for legacy DBs (null `last_accessed_at` = "no decay yet").
  - `FRESHNESS_HALF_LIFE_SECONDS = 30 * 86_400` (30 days; chunks halve in freshness when untouched for a month).
  - `FRESHNESS_EWMA_ALPHA = 0.5` (one touch pulls halfway toward 1.0).
  - `freshness_of(id, now)` — pure read; applies `0.5 ** (elapsed / half_life)` decay over stored value.
  - `touch_chunk(id, now)` — `new = alpha + (1 - alpha) * decayed_old`; updates `freshness` + `last_accessed_at`.
  - `entity_aware_freshness(id, now)` — `max(freshness_of(id), 0.5 ** ((now - max_entity_last_seen) / half_life))`. Linked-entity activity refreshes the chunk via the provenance edges from P-15.
  - `_decayed(stored, last_seen, now, half_life)` standalone helper.
  - `add_chunk` now accepts `now` for deterministic tests; persists `freshness=1.0` + `last_accessed_at=now`.
- `sera/memory/search.py`:
  - `hybrid_search` gains `apply_freshness=True`, `touch=True`, `now` kwargs.
  - Post-RRF, each candidate's fused score is multiplied by `tree.entity_aware_freshness(cid, now)` and the list re-sorted.
  - Every returned hit gets `touch_chunk` called — retrieval reinforces freshness.
  - `apply_freshness=False` / `touch=False` knobs for tests + read-only callers.

## Files touched

`sera/memory/tree.py`, `sera/memory/search.py`; new `tests/test_freshness.py` (19 tests).

## Verification

```bash
pytest -q tests/test_freshness.py     # 19 passed
pytest -q                              # 250 passed total (was 231 + 19 new)
python -m pyflakes sera/               # 0 warnings
```

## Dependencies

P-11.

## Notes

_Journal: decisions, blockers, commit refs go here._

**Design decisions (2026-05-21):**

- **True half-life math, not `exp(-t/τ)`.** First implementation used `math.exp(-elapsed / half_life)` which would have made the constant a *time constant* (~37% at one period), not a half-life. Test caught the divergence — switched to `0.5 ** (elapsed / half_life)` so the docstring contract holds. Lock it via `test_half_life_constants_unchanged` + `test_decay_factor_matches_half_life_formula`.
- **EWMA-on-touch, not pure decay.** Each touch is `new = α + (1-α) * decayed_old`. A single touch on a fully-decayed chunk lifts it to ~α=0.5, not all the way to 1.0. Five touches in quick succession asymptote to ~1.0. This makes one-off recalls less authoritative than sustained repeated access — a noisy single hit doesn't pretend the chunk is fresh forever.
- **Outclass: entity-aware boost via provenance.** Direct chunk decay is the baseline; entity-aware takes `max(direct, decayed_max_entity_last_seen)`. A 2-year-old chunk whose linked entity (Alice) was mentioned yesterday still scores near 1.0 because Alice is alive. Test: `test_entity_aware_freshness_lifts_old_chunk_with_active_entity`. Rivals decay docs uniformly by created_at — Sera weights recency by *which entities are still active*.
- **`max`, not `sum` or `mean`.** Combining entity decay with chunk decay multiplicatively or additively would over-boost chunks linked to many entities. Max is the conservative join: a chunk is as fresh as its most-active context, not the average of all contexts.
- **Touch on every search hit, default-on.** Retrieval *is* a recall event; the chunk just got used. Skipping the touch would let freshness drift toward 0 across reads, defeating the purpose. The `touch=False` knob exists for read-only audits.
- **Null `last_accessed_at` = no decay.** Legacy DBs migrate with NULL — the `_decayed` helper short-circuits to the stored value, so legacy chunks don't get penalized for the migration timestamp. Their first real touch sets the column properly.
- **30-day half-life is the right scale.** Daily decay (24h) burns through useful long-term context. Yearly is functionally infinite for an active agent. 30 days = monthly cadence — facts older than a quarter need to come up in conversation or get demoted.
- **Freshness multiplies, doesn't replace, the RRF score.** A chunk with score 0.5 and freshness 0.5 ranks at 0.25; one with score 0.4 and freshness 0.9 ranks at 0.36. Multiplicative fusion preserves relative orderings within each freshness tier while still demoting stale top-RRF hits — exactly what we want.
- **No automatic cleanup.** Stale chunks never get deleted. The phase goal is explicit: "demoted; never deleted." Removal is a user-driven action (vault edit, archive flag — not in scope here). Retention discipline stays the user's call.
