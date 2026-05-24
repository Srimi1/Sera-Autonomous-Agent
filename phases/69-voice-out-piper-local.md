# P-69 — Voice out (piper local)

## Status

done (adapter verified with injected runner; real binary not present).

## Outclass claim

**Offline TTS.** piper synthesizes speech on-device from a local ONNX voice
model — no cloud TTS, no per-character billing, works airgapped. Like STT, it
**never falls back to a network service**: no piper → TTSUnavailable.

## Files

- `sera/voice/tts.py` — LocalTTS (piper)
- `tests/test_voice.py::TestLocalTTS`

## Verification

| Check | Status | Notes |
|-------|--------|-------|
| LocalTTS tested | ✅ | 5 tests via injected runner |
| synthesizes via piper | ✅ | text on stdin, --model + --output_file passed |
| empty text rejected | ✅ | TTSError |
| failure surfaced | ✅ | piper exit≠0 → TTSError |
| **no-cloud guarantee** | ✅ | no piper → TTSUnavailable |

## Limits

- **No real audio produced.** piper binary + voice model not installed; tests
  use an injected runner. The airgap-reply-audible verification needs a machine
  with piper + a `.onnx` voice. Command-building + failure paths are tested.
- Not wired into the turn loop as an output sink yet (it's a library).

## Dependencies

P-61.
