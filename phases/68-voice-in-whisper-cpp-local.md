# P-68 — Voice in (whisper.cpp local)

## Status

done (adapter verified with injected runner; real binary not present).

## Outclass claim

**Works offline on a plane.** OpenHuman's voice is external-API only — no
network, no transcription. Sera transcribes entirely on-device (mlx-whisper on
Apple Silicon, whisper.cpp elsewhere) and **never falls back to the cloud**: if
no local engine exists it raises STTUnavailable rather than silently shipping
your audio to a server.

## Files

- `sera/voice/stt.py` — LocalSTT (mlx-whisper preferred, whisper.cpp fallback)
- `tests/test_voice.py::TestLocalSTT`

## Verification

| Check | Status | Notes |
|-------|--------|-------|
| LocalSTT tested | ✅ | 7 tests via injected runner |
| whisper.cpp JSON parsed | ✅ | segments → text, language extracted |
| plain-text fallback | ✅ | non-JSON stdout handled |
| mlx preferred when present | ✅ | engine selection correct |
| **no-cloud guarantee** | ✅ | no engine → STTUnavailable, never a network call |

## Limits

- **No real transcription performed.** whisper.cpp / mlx-whisper binaries +
  models aren't installed here; all tests use an injected runner. The actual
  airgap-dictation → text verification needs a machine with the engine + a
  model. Command-building, output-parsing, engine-selection, and the no-cloud
  guarantee are tested.
- Not wired into the gateway/turn loop as an input source yet (it's a library).

## Dependencies

P-61.
