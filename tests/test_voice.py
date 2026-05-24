"""Tests for sera.voice — local STT (P-68) and TTS (P-69).

The real binaries (whisper.cpp, piper) aren't present here; the command-building,
output-parsing, offline-fallback, and never-use-cloud behavior are tested with
injected runners — the same seam used for the iMessage osascript runner.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from sera.voice.stt import LocalSTT, STTError, STTUnavailable, Transcript
from sera.voice.tts import LocalTTS, Speech, TTSError, TTSUnavailable


# ---------------------------------------------------------------------------
# P-68 — STT
# ---------------------------------------------------------------------------

class TestLocalSTT:
    def _audio(self, tmp_path: Path) -> Path:
        p = tmp_path / "clip.wav"
        p.write_bytes(b"RIFF....WAVE")   # not real audio; runner is faked
        return p

    def test_unavailable_when_no_engine(self, tmp_path: Path) -> None:
        stt = LocalSTT(whisper_cpp_bin="", mlx_available=False)
        with pytest.raises(STTUnavailable):
            stt.transcribe(self._audio(tmp_path))

    def test_missing_audio_raises(self, tmp_path: Path) -> None:
        stt = LocalSTT(whisper_cpp_bin="/usr/bin/whisper-cli", mlx_available=False)
        with pytest.raises(STTError):
            stt.transcribe(tmp_path / "nope.wav")

    def test_whisper_cpp_json_parsed(self, tmp_path: Path) -> None:
        captured: list[list[str]] = []

        def runner(cmd):
            captured.append(cmd)
            payload = {"transcription": [{"text": "hello "}, {"text": "world"}],
                       "result": {"language": "en"}}
            return 0, json.dumps(payload), ""

        stt = LocalSTT(whisper_cpp_bin="/usr/bin/whisper-cli", mlx_available=False, _runner=runner)
        result = stt.transcribe(self._audio(tmp_path))
        assert result.text == "hello world"
        assert result.engine == "whisper.cpp"
        assert result.language == "en"
        assert "-f" in captured[0]      # audio file flag was passed

    def test_whisper_cpp_plain_text_fallback(self, tmp_path: Path) -> None:
        def runner(cmd):
            return 0, "just plain text", ""

        stt = LocalSTT(whisper_cpp_bin="/usr/bin/whisper-cli", mlx_available=False, _runner=runner)
        assert stt.transcribe(self._audio(tmp_path)).text == "just plain text"

    def test_whisper_cpp_failure_raises(self, tmp_path: Path) -> None:
        def runner(cmd):
            return 1, "", "model not found"

        stt = LocalSTT(whisper_cpp_bin="/usr/bin/whisper-cli", mlx_available=False, _runner=runner)
        with pytest.raises(STTError):
            stt.transcribe(self._audio(tmp_path))

    def test_mlx_preferred_over_whisper_cpp(self, tmp_path: Path) -> None:
        def runner(cmd):
            assert cmd[0] == "mlx_whisper"     # mlx wins when both present
            return 0, json.dumps({"text": "from mlx", "language": "en"}), ""

        stt = LocalSTT(whisper_cpp_bin="/usr/bin/whisper-cli", mlx_available=True, _runner=runner)
        assert stt.engine_name() == "mlx-whisper"
        assert stt.transcribe(self._audio(tmp_path)).text == "from mlx"

    def test_available_reports_engines(self) -> None:
        assert LocalSTT(whisper_cpp_bin="", mlx_available=False).available() is False
        assert LocalSTT(whisper_cpp_bin="/x/whisper-cli", mlx_available=False).available() is True


# ---------------------------------------------------------------------------
# P-69 — TTS
# ---------------------------------------------------------------------------

class TestLocalTTS:
    def test_unavailable_when_no_piper(self, tmp_path: Path) -> None:
        tts = LocalTTS(voice_model="en_US.onnx", piper_bin="")
        with pytest.raises(TTSUnavailable):
            tts.synthesize("hi", tmp_path / "out.wav")

    def test_empty_text_raises(self, tmp_path: Path) -> None:
        tts = LocalTTS(voice_model="v.onnx", piper_bin="/usr/bin/piper")
        with pytest.raises(TTSError):
            tts.synthesize("   ", tmp_path / "out.wav")

    def test_synthesizes_via_piper(self, tmp_path: Path) -> None:
        captured: dict = {}

        def runner(cmd, stdin_text):
            captured["cmd"] = cmd
            captured["text"] = stdin_text
            return 0, ""

        tts = LocalTTS(voice_model="en_US.onnx", piper_bin="/usr/bin/piper", _runner=runner)
        out = tmp_path / "out.wav"
        result = tts.synthesize("hello there", out)
        assert result.engine == "piper"
        assert result.out_path == out
        assert captured["text"] == "hello there"
        assert "--model" in captured["cmd"]
        assert "en_US.onnx" in captured["cmd"]

    def test_piper_failure_raises(self, tmp_path: Path) -> None:
        def runner(cmd, stdin_text):
            return 1, "voice model missing"

        tts = LocalTTS(voice_model="v.onnx", piper_bin="/usr/bin/piper", _runner=runner)
        with pytest.raises(TTSError):
            tts.synthesize("hi", tmp_path / "out.wav")

    def test_engine_name(self) -> None:
        assert LocalTTS(voice_model="v", piper_bin="/x/piper").engine_name() == "piper"
        assert LocalTTS(voice_model="v", piper_bin="").engine_name() is None
