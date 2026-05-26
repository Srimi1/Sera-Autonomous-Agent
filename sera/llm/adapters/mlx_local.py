"""MLX local adapter — P-74.

OUTCLASS: The bandit router can pick this adapter for routine tasks and serve
them with zero API calls.  Anthropic, OpenAI — they have no local slot in the
router.  Sera does.

The adapter wraps mlx-lm's `generate` command.  Since subprocess output is
synchronous, it runs the process in a thread executor and yields one StreamChunk
with the full response.  That's enough for the outclass: zero API cost per call,
fully offline, wired into the existing bandit routing loop.

Tool calls are not supported — mlx-lm generate is a text-completion primitive.
The router must only select mlx_local for task_kinds that don't require tool use
(e.g. summarise, classify, explain).  The adapter signals this by yielding
finish_reason="stop" and never yielding a tool_call_delta.

mlx-lm generate command
------------------------
python -m mlx_lm.generate \\
    --model  <base_model> \\
    [--adapter-path <adapter_dir>] \\
    --prompt <chatml_text> \\
    --max-tokens <n>

Output: the generated text is written to stdout, possibly preceded by
"Prompt: <text>" — we strip that prefix.
"""
from __future__ import annotations

import asyncio
import logging
import re
import sys
from pathlib import Path
from typing import Any, AsyncIterator, Callable

from sera.llm.base import StreamChunk

log = logging.getLogger("sera.llm.adapters.mlx_local")

Runner = Callable[[list[str]], tuple[int, str, str]]   # cmd → (rc, stdout, stderr)

# mlx-lm prints "Prompt: <text>\n" before the generation on some versions.
_PROMPT_PREFIX_RE = re.compile(r"^Prompt:\s*.*?\n", re.DOTALL)


# ---------------------------------------------------------------------------
# ChatML prompt builder
# ---------------------------------------------------------------------------

_CHATML_SYSTEM  = "<|im_start|>system\n{content}\n<|im_end|>\n"
_CHATML_USER    = "<|im_start|>user\n{content}\n<|im_end|>\n"
_CHATML_ASST    = "<|im_start|>assistant\n{content}\n<|im_end|>\n"
_CHATML_ASST_OPEN = "<|im_start|>assistant\n"


def _build_chatml_prompt(
    messages: list[dict[str, Any]],
    system: str | None = None,
) -> str:
    """Convert OpenAI-style messages to a ChatML prompt string."""
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
        # tool results: fold into user turn for simplicity
        elif role == "tool":
            parts.append(_CHATML_USER.format(content=f"[tool result] {content}"))
    parts.append(_CHATML_ASST_OPEN)
    return "".join(parts)


# ---------------------------------------------------------------------------
# Default runner
# ---------------------------------------------------------------------------

def _default_runner(cmd: list[str]) -> tuple[int, str, str]:
    import subprocess
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode, result.stdout, result.stderr


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class MLXLocalAdapter:
    """mlx-lm backed LLM adapter.  Zero API cost; runs on Apple Silicon."""

    name = "mlx_local"
    context_budget = 32_768

    def __init__(
        self,
        base_model: str = "mlx-community/Mistral-7B-Instruct-v0.2-4bit",
        adapter_path: Path | str | None = None,
        max_tokens: int = 512,
        runner: Runner | None = None,
    ) -> None:
        self.model = base_model
        self._adapter = Path(adapter_path) if adapter_path else None
        self._max_tokens = max_tokens
        self._run = runner or _default_runner

    # ------------------------------------------------------------------
    # Command builder (pure, no side-effects)
    # ------------------------------------------------------------------

    def build_cmd(self, prompt: str) -> list[str]:
        cmd = [
            sys.executable, "-m", "mlx_lm.generate",
            "--model",      self.model,
            "--prompt",     prompt,
            "--max-tokens", str(self._max_tokens),
        ]
        if self._adapter and self._adapter.exists():
            cmd += ["--adapter-path", str(self._adapter)]
        return cmd

    # ------------------------------------------------------------------
    # Output parser
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_output(raw: str) -> str:
        """Strip mlx-lm preamble; return generated text only."""
        text = _PROMPT_PREFIX_RE.sub("", raw, count=1).strip()
        # Some versions wrap output in =====\n ... \n=====
        if text.startswith("="):
            lines = text.splitlines()
            inner = [line for line in lines if not line.startswith("===")]
            text = "\n".join(inner).strip()
        return text

    # ------------------------------------------------------------------
    # Availability check
    # ------------------------------------------------------------------

    def available(self) -> bool:
        """Return True if mlx-lm can be imported (mlx installed)."""
        try:
            import importlib.util
            return importlib.util.find_spec("mlx_lm") is not None
        except Exception:
            return False

    # ------------------------------------------------------------------
    # LLM Protocol — async stream
    # ------------------------------------------------------------------

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        system: str | None = None,
    ) -> AsyncIterator[StreamChunk]:
        prompt = _build_chatml_prompt(messages, system=system)
        cmd = self.build_cmd(prompt)
        log.debug("mlx_local generate: model=%s adapter=%s", self.model, self._adapter)

        loop = asyncio.get_event_loop()
        rc, stdout, stderr = await loop.run_in_executor(None, self._run, cmd)

        if rc != 0:
            err = (stderr or stdout or "mlx_lm.generate failed").strip()
            log.warning("mlx_local generate failed rc=%d: %s", rc, err[:200])
            yield StreamChunk(delta_text=f"[mlx_local error: {err[:120]}]",
                              finish_reason="stop",
                              usage={"input_tokens": 0, "output_tokens": 0})
            return

        text = self._parse_output(stdout)
        # Estimate token counts from char length — no official count from subprocess
        in_tokens  = max(1, len(prompt) // 4)
        out_tokens = max(1, len(text) // 4)
        yield StreamChunk(
            delta_text=text,
            finish_reason="stop",
            usage={"input_tokens": in_tokens, "output_tokens": out_tokens},
        )
