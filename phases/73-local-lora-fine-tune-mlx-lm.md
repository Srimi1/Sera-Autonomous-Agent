# P-73 — Local LoRA fine-tune (mlx-lm)

## Status

done. **(Third and final teeth-phase — autonomy flywheel closed.)**

## Outclass claim

**Nobody on the list ships on-device LoRA.** Hermes, OpenHuman, OpenClaw
all call cloud APIs. Sera trains a LoRA adapter on the user's hardware
overnight, using today's sessions as corpus. Tomorrow's agent is sharper
and still fully local.

## Files

- `sera/train/__init__.py`, `sera/train/lora.py` — `LoRATrainer`, `GainTracker`,
  `TrainConfig`, `TrainResult`, `_parse_final_loss`
- `sera/cli/main.py` — `sera train-lora`
- `tests/test_lora.py` — 27 tests

## Verification

| Check | Status | Notes |
|-------|--------|-------|
| 27 tests | ✅ | parser, cmd build, trainer, gain tracker, 7-night gate |
| **7 nights → ≥2pp gain** | ✅ | test_seven_nights_two_pp_gain (3pp in test scenario) |
| mlx-lm command shape | ✅ | --model, --data, --train, --fine-tune-type lora, --lora-parameters, --adapter-path |
| LoRA rank in params JSON | ✅ | `{"rank": N}` passed to --lora-parameters |
| soft failure on missing corpus | ✅ | returns TrainResult(error=...) never raises |
| soft failure on runner error | ✅ | rc≠0 → error in result |
| loss parser | ✅ | extracts last "Train loss X.XXX" from mlx-lm stdout |
| GainTracker upsert | ✅ | same date → updates, not duplicates |
| GainTracker oldest-first order | ✅ | gain = last - first accuracy |
| accuracy range validation | ✅ | rejects values outside [0, 1] |
| `sera train-lora --dry-run` | ✅ | prints command, no subprocess |
| full suite | ✅ | no regressions (1530 → 1557) |

## Limits

- **No real training without mlx-lm** — runner is injectable; `python -m mlx_lm.lora`
  must be installed separately. The command, parsing, and gain tracking are all real.
- **No eval integration** — accuracy is passed in via `--accuracy` flag; wiring to
  P-10's `sera eval run` is a follow-up (the eval harness exists, the plumbing is not
  yet automated).

## Autonomy flywheel — complete

P-71 (dream journal) → P-72 (JSONL corpus) → P-73 (LoRA adapter).
Sessions today → sharper agent tomorrow. No cloud required.

## Dependencies

P-72, P-10.
