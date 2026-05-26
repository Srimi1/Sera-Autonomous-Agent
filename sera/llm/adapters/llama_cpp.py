"""llama-cpp-python adapter — P-93.

OUTCLASS: Cloud is opt-in; local is default for private tasks.  This adapter
routes any task marked `privacy=high` to a local Phi-3/Qwen/Llama model via
llama-cpp-python.  Zero API calls, fully airgapped.

llama-cpp-python CLI
--------------------
python -m llama_cpp.server is the HTTP server.  For single-shot generation
we call the Python API directly when available, or shell out to:

    python -c "from llama_cpp import Llama; ..."

Using the injectable runner seam (same pattern as MLX adapter) so tests
never need the binary installed.

Supported models
----------------
Any GGUF file: Phi-3-mini-4k-instruct, Qwen2-7B, Llama-3.2-3B, etc.
Default path: ~/.sera/models/default.gguf

ChatML prompt
-------------
Uses the same _build_chatml_prompt helper as the MLX adapter.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import Any, AsyncIterator, Callable

from sera.llm.base import StreamChunk

log = logging.getLogger("sera.llm.adapters.llama_cpp")

Runner = Callable[[list[str]], tuple[int, str, str]]

_DEFAULT_MODEL = Path.home() / ".sera" / "models" / "default.gguf"
_DEFAULT_MAX_TOKENS = 512
_DEFAULT_CTX = 4096


# ---------------------------------------------------------------------------
# ChatML (re-used from mlx_local pattern)
# ---------------------------------------------------------------------------

_CHATML_SYSTEM   = "<|im_start|>system\n{content}\n<|im_end|>\n"
_CHATML_USER     = "<|im_start|>user\n{content}\n<|im_end|>\n"
_CHATML_ASST     = "<|im_start|>assistant\n{content}\n<|im_end|>\n"
_CHATML_ASST_OPEN = "<|im_start|>assistant\n"


def _build_chatml_prompt(
    messages: list[dict[str, Any]],
    system: str | None = None,
) -> str:
    parts: list[str] = []
    if system:
        parts.append(_CHATML_SYSTEM.format(content=system))
    for m in messages:
        role = m.get("role", "")
        content = str(m.get("content") or "")
        if role == "system":
            parts.append(_CHATML_SYSTEM.format(content=content))
        elif role == "user":
            parts.append(_CHATML_USER.format(content=content))
        elif role == "assistant":
            parts.append(_CHATML_ASST.format(content=content))
        elif role == "tool":
            parts.append(_CHATML_USER.format(content=f"[tool result] {content}"))
    parts.append(_CHATML_ASST_OPEN)
    return "".join(parts)


# ---------------------------------------------------------------------------
# Default subprocess runner
# ---------------------------------------------------------------------------

def _default_runner(cmd: list[str]) -> tuple[int, str, str]:
    import subprocess
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    return result.returncode, result.stdout, result.stderr


# ---------------------------------------------------------------------------
# Output parser
# ---------------------------------------------------------------------------

def _parse_output(raw: str) -> str:
    """Strip llama-cpp preamble lines from raw stdout."""
    lines = raw.splitlines()
    out_lines: list[str] = []
    skip_prefixes = ("llama_", "ggml_", "load ", "Loaded ", "system_info", "sampling:")
    in_output = False
    for line in lines:
        stripped = line.strip()
        if not in_output:
            if any(stripped.startswith(p) for p in skip_prefixes):
                continue
            if stripped.startswith("<|im_start|>") or stripped.startswith("Prompt:"):
                continue
            in_output = True
        out_lines.append(line)
    text = "\n".join(out_lines).strip()
    # Strip trailing <|im_end|> if model echoes it
    if text.endswith("<|im_end|>"):
        text = text[: -len("<|im_end|>")].rstrip()
    return text


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class LlamaCppAdapter:
    """Local GGUF model via llama-cpp-python subprocess.

    Parameters
    ----------
    model_path:    Path to a .gguf model file.
    max_tokens:    Token budget for generation.
    n_ctx:         Context window size.
    runner:        Injectable subprocess runner (default: real subprocess).
    """

    name = "llama_cpp"
    context_budget = _DEFAULT_CTX

    def __init__(
        self,
        model_path: Path | str | None = None,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
        n_ctx: int = _DEFAULT_CTX,
        runner: Runner | None = None,
    ) -> None:
        self._model_path = Path(model_path) if model_path else _DEFAULT_MODEL
        self._max_tokens = max_tokens
        self._n_ctx = n_ctx
        self._runner = runner or _default_runner
        self.calls = 0

    def build_cmd(self, prompt: str) -> list[str]:
        """Build the shell command that runs llama-cpp generation."""
        script = (
            "from llama_cpp import Llama;"
            f"m=Llama(model_path={str(self._model_path)!r},"
            f"n_ctx={self._n_ctx},verbose=False);"
            f"out=m.create_completion({prompt!r},max_tokens={self._max_tokens},stop=['<|im_end|>']);"
            "print(out['choices'][0]['text'])"
        )
        return [sys.executable, "-c", script]

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        system: str | None = None,
    ) -> AsyncIterator[StreamChunk]:
        prompt = _build_chatml_prompt(messages, system)
        cmd = self.build_cmd(prompt)
        loop = asyncio.get_event_loop()
        rc, stdout, stderr = await loop.run_in_executor(
            None, self._runner, cmd
        )
        self.calls += 1
        if rc != 0:
            log.warning("llama_cpp exited %d: %s", rc, stderr[:200])
        text = _parse_output(stdout) if stdout.strip() else ""
        yield StreamChunk(delta_text=text, finish_reason="stop")
