"""Voice-out — local text-to-speech via piper.

OUTCLASS: offline TTS. piper synthesizes speech entirely on-device from a local
ONNX voice model — no cloud TTS, no per-character billing, works airgapped.

The synthesis backend is injectable (`_runner`) so command-building and the
unavailable-engine path are testable without the piper binary or a voice model.
`synthesize()` raises TTSUnavailable (never silently no-ops) when piper is
absent.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

log = logging.getLogger("sera.voice.tts")

# runner(cmd, stdin_text) -> (returncode, stderr)
RunnerResult = tuple[int, str]
Runner = Callable[[list[str], str], RunnerResult]


class TTSUnavailable(RuntimeError):
    """piper is not installed. We do NOT fall back to a cloud TTS service."""


class TTSError(RuntimeError):
    """piper ran but synthesis failed."""


@dataclass(frozen=True)
class Speech:
    out_path: Path
    engine: str


def _default_runner(cmd: list[str], stdin_text: str) -> RunnerResult:
    try:
        proc = subprocess.run(
            cmd, input=stdin_text.encode("utf-8"),
            capture_output=True, timeout=120,
        )
        return proc.returncode, proc.stderr.decode("utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)


class LocalTTS:
    """On-device speech synthesis via piper."""

    def __init__(
        self,
        *,
        voice_model: str | Path,
        piper_bin: str | None = None,
        _runner: Runner | None = None,
    ) -> None:
        self._voice = str(voice_model)
        self._piper = piper_bin if piper_bin is not None else shutil.which("piper")
        self._runner = _runner or _default_runner

    def available(self) -> bool:
        return bool(self._piper)

    def engine_name(self) -> str | None:
        return "piper" if self._piper else None

    def synthesize(self, text: str, out_path: Path | str) -> Speech:
        if not text.strip():
            raise TTSError("nothing to synthesize (empty text)")
        if not self.available():
            raise TTSUnavailable(
                "piper not found on PATH. Install piper + a voice .onnx model. "
                "Sera does not use cloud TTS."
            )
        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        # piper --model <voice.onnx> --output_file <out.wav>  (text on stdin)
        cmd = [self._piper or "piper", "--model", self._voice, "--output_file", str(out)]
        rc, err = self._runner(cmd, text)
        if rc != 0:
            raise TTSError(f"piper failed: {err.strip() or f'exit {rc}'}")
        return Speech(out_path=out, engine="piper")
