# P-74 — Local LoRA adapter for routing

## Status

done.

## Outclass claim

**Local model in the router — bandit can pick it.** Hermes/OpenHuman have
no local slot in their routers. Sera's Thompson-sampling bandit can route
routine `summarise` / `classify` task_kinds to the mlx-lm adapter and serve
them with zero API cost and zero network calls.

## Files

- `sera/llm/adapters/mlx_local.py` — `MLXLocalAdapter`, `_build_chatml_prompt`,
  `_parse_output`
- `sera/llm/router.py` — extended `build_llm` to recognise `provider="mlx_local"`
- `tests/test_mlx_local.py` — 24 tests

## Verification

| Check | Status | Notes |
|-------|--------|-------|
| 24 tests | ✅ | prompt builder, cmd, parser, stream, router, zero-cost gate |
| **zero API cost** | ✅ | test_mlx_local_records_zero_cost (cost_usd=0.0 in router_stats) |
| **zero network calls** | ✅ | test_stream_produces_no_http_call (socket.connect not called) |
| ChatML prompt format | ✅ | system / user / assistant / tool-result turns |
| adapter_path included when dir exists | ✅ | absent when path missing |
| soft failure on bad rc | ✅ | error chunk yielded, no raise |
| router build_llm dispatches mlx_local | ✅ | unknown provider raises ValueError |
| full suite | ✅ | no regressions (1557 → 1581) |

## Limits

- **mlx-lm must be installed** — the runner is injectable for tests; production
  requires `pip install mlx-lm` on Apple Silicon.
- **No tool use** — mlx-lm generate is text-only. Router must gate mlx_local to
  task_kinds that don't require tools.
- **No streaming** — subprocess output is returned atomically, yielded as one chunk.

## Dependencies

P-73, P-37.
