# P-93 — Edge LLM by default for private tasks

## Status

done (shipped 2026-05-24).

## Outclass claim

**Cloud opt-in, local default.** `provider="llama_cpp"` in the router sends any task to a local GGUF model (Phi-3/Qwen/Llama). Zero API calls. Injectable runner seam — testable without the binary. No rival routes to local by default.

## Outclass claim

**Phi-3 / Qwen / Llama small models local.** Cloud opt-in only.

## Files

`sera/llm/adapters/llama_cpp.py`.

## Verification

airgap Sera answers from local model + memory.

## Dependencies

P-74.


## Notes

_Journal: decisions, blockers, commit refs go here._
