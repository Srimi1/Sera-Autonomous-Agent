# P-65 — Memory Tree browser

## Status

done (backend verified; React written, not run).

## Outclass claim

**Entity graph view with provenance breadcrumbs.** Every relation Sera shows
links back to the chunk that asserted it — you see not just "Alice works_at
OpenAI" but *why* Sera believes it (source + summary + confidence). Rivals show
a memory list; Sera shows the evidence chain.

## Files

- `sera/shell/viewmodels.py::entity_card` — tested backend
- `sera-shell/src/components/MemoryTree.tsx` — consumer (not run here)
- `tests/test_shell_viewmodels.py::TestEntityCard`

## Verification

| Check | Status | Notes |
|-------|--------|-------|
| entity_card tested | ✅ | 5 tests |
| search "Alice" → relations | ✅ | test_search_alice_yields_relations |
| **provenance breadcrumb** | ✅ | test_provenance_breadcrumb_present — relation carries its source chunk |
| unknown entity → None | ✅ | clean miss |
| full suite | ✅ | no regressions |

## Limits

- **MemoryTree.tsx not executed** (no Tauri/Vite); written as real fetch code.
- **`GET /v1/memory/entity` HTTP endpoint not yet wired** — view-model tested
  directly; endpoint wiring is the same deferred-integration class as the P-64
  approval transport.

## Dependencies

P-15, P-61.
