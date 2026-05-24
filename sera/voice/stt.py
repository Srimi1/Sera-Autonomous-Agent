"""Voice-in — local speech-to-text via whisper.cpp / mlx-whisper.

OUTCLASS: works offline on a plane. OpenHuman's voice is external-API only —
no network, no transcription. Sera transcribes entirely on-device: whisper.cpp
(CPU/Metal) or mlx-whisper on Apple Silicon. Nothing leaves the machine, so it
works airgapped and never bills a cloud STT endpoint.

The transcription backend is injectable (`_runner`) so the command-building,
output-parsing, and offline-fallback logic is testable without the real binary
or a model present. `transcribe()` raises STTUnavailable with an actionable
message when no local engine is installed — never silently falls back to a
network service.
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

log = logging.getLogger("sera.voice.stt")

# runner(cmd: list[str]) -> (returncode, stdout, stderr)
RunnerResult = tuple[int, str, str]
Runner = Callable[[list[str]], RunnerResult]

DEFAULT_MODEL = "base.en"


class STTUnavailable(RuntimeError):
    """No local STT engine is installed. We do NOT fall back to the cloud."""


class STTError(RuntimeError):
    """A local engine ran but failed to transcribe."""


@dataclass(frozen=True)
class Transcript:
    text: str
    engine: str
    language: str | None = None


def _default_runner(cmd: list[str]) -> RunnerResult:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        return proc.returncode, proc.stdout, proc.stderr
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, "", str(exc)


def _which(name: str) -> str | None:
    return shutil.which(name)


class LocalSTT:
    """On-device transcription. Prefers mlx-whisper, falls back to whisper.cpp."""

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        whisper_cpp_bin: str | None = None,
        mlx_available: bool | None = None,
        _runner: Runner | None = None,
    ) -> None:
        self._model = model
        # Allow explicit injection; otherwise probe PATH.
        self._whisper_cpp = whisper_cpp_bin if whisper_cpp_bin is not None else _which("whisper-cli") or _which("whisper-cpp")
        self._mlx = mlx_available if mlx_available is not None else _probe_mlx()
        self._runner = _runner or _default_runner

    def available(self) -> bool:
        return bool(self._mlx or self._whisper_cpp)

    def engine_name(self) -> str | None:
        if self._mlx:
            return "mlx-whisper"
        if self._whisper_cpp:
            return "whisper.cpp"
        return None

    def transcribe(self, audio_path: Path | str) -> Transcript:
        audio = Path(audio_path)
        if not audio.exists():
            raise STTError(f"audio file not found: {audio}")
        if not self.available():
            raise STTUnavailable(
                "No local STT engine found. Install mlx-whisper (Apple Silicon) "
                "or whisper.cpp (whisper-cli on PATH). Sera does not use cloud STT."
            )

        if self._mlx:
            return self._transcribe_mlx(audio)
        return self._transcribe_whisper_cpp(audio)

    # -- engine: whisper.cpp -----------------------------------------------

    def _transcribe_whisper_cpp(self, audio: Path) -> Transcript:
        # whisper-cli -m <model> -f <audio> -oj (json to stdout)
        cmd = [self._whisper_cpp or "whisper-cli", "-m", self._model, "-f", str(audio), "-oj", "-np"]
        rc, out, err = self._runner(cmd)
        if rc != 0:
            raise STTError(f"whisper.cpp failed: {err.strip() or f'exit {rc}'}")
        text, lang = _parse_whisper_cpp(out)
        return Transcript(text=text, engine="whisper.cpp", language=lang)

    # -- engine: mlx-whisper -----------------------------------------------

    def _transcribe_mlx(self, audio: Path) -> Transcript:
        cmd = ["mlx_whisper", "--model", self._model, "--output-format", "json", str(audio)]
        rc, out, err = self._runner(cmd)
        if rc != 0:
            raise STTError(f"mlx-whisper failed: {err.strip() or f'exit {rc}'}")
        text, lang = _parse_mlx(out)
        return Transcript(text=text, engine="mlx-whisper", language=lang)


def _probe_mlx() -> bool:
    try:
        import importlib.util
        return importlib.util.find_spec("mlx_whisper") is not None
    except Exception:  # noqa: BLE001
        return False


def _parse_whisper_cpp(stdout: str) -> tuple[str, str | None]:
    """whisper.cpp -oj prints JSON: {transcription: [{text}], result:{language}}.

    Falls back to treating stdout as raw text if it isn't JSON (some builds
    print plain text to stdout and JSON to a file).
    """
    stdout = stdout.strip()
    if not stdout:
        return "", None
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return stdout, None
    segments = data.get("transcription") or []
    text = "".join(seg.get("text", "") for seg in segments).strip()
    if not text and isinstance(data.get("text"), str):
        text = data["text"].strip()
    lang = (data.get("result") or {}).get("language")
    return text, lang


def _parse_mlx(stdout: str) -> tuple[str, str | None]:
    stdout = stdout.strip()
    if not stdout:
        return "", None
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return stdout, None
    return str(data.get("text", "")).strip(), data.get("language")
