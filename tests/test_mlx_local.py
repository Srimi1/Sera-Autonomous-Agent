"""Tests for sera.llm.adapters.mlx_local — P-74 Local LoRA adapter for routing.

Phase verification: a summarise turn served by mlx_local records cost_usd=0.0
and provider="mlx_local" in router_stats — zero API call.
No mlx-lm binary required; injectable runner throughout.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from sera.llm.adapters.mlx_local import (
    MLXLocalAdapter,
    _build_chatml_prompt,
    _CHATML_ASST_OPEN,
)
from sera.llm.base import StreamChunk
from sera.llm.router import build_llm
from sera.llm.router_stats import record_call, p50_table


# ---------------------------------------------------------------------------
# Stub runners
# ---------------------------------------------------------------------------

def _ok_runner(text: str):
    def run(cmd: list[str]) -> tuple[int, str, str]:
        return 0, text, ""
    return run


def _fail_runner(cmd: list[str]) -> tuple[int, str, str]:
    return 1, "", "OOM: device out of memory"


# ---------------------------------------------------------------------------
# _build_chatml_prompt
# ---------------------------------------------------------------------------

class TestBuildChatMLPrompt:
    def test_ends_with_assistant_open(self) -> None:
        msgs = [{"role": "user", "content": "Hello"}]
        prompt = _build_chatml_prompt(msgs)
        assert prompt.endswith(_CHATML_ASST_OPEN)

    def test_user_content_included(self) -> None:
        msgs = [{"role": "user", "content": "What is Sera?"}]
        prompt = _build_chatml_prompt(msgs)
        assert "What is Sera?" in prompt

    def test_system_prepended(self) -> None:
        msgs = [{"role": "user", "content": "Hi"}]
        prompt = _build_chatml_prompt(msgs, system="You are Sera.")
        assert prompt.startswith("<|im_start|>system\nYou are Sera.")

    def test_assistant_turn_included(self) -> None:
        msgs = [
            {"role": "user",      "content": "Q"},
            {"role": "assistant", "content": "A"},
            {"role": "user",      "content": "Q2"},
        ]
        prompt = _build_chatml_prompt(msgs)
        assert "<|im_start|>assistant\nA\n<|im_end|>" in prompt

    def test_tool_result_folded_into_user(self) -> None:
        msgs = [
            {"role": "user",  "content": "run it"},
            {"role": "tool",  "content": "done", "tool_call_id": "x"},
        ]
        prompt = _build_chatml_prompt(msgs)
        assert "[tool result] done" in prompt

    def test_system_message_in_list(self) -> None:
        msgs = [{"role": "system", "content": "Be concise."},
                {"role": "user",   "content": "Q"}]
        prompt = _build_chatml_prompt(msgs)
        assert "<|im_start|>system\nBe concise." in prompt


# ---------------------------------------------------------------------------
# MLXLocalAdapter.build_cmd
# ---------------------------------------------------------------------------

class TestBuildCmd:
    def test_contains_mlx_lm_generate(self, tmp_path: Path) -> None:
        a = MLXLocalAdapter(base_model="mlx-community/test")
        cmd = a.build_cmd("hello")
        assert "mlx_lm.generate" in " ".join(cmd)

    def test_model_in_cmd(self, tmp_path: Path) -> None:
        a = MLXLocalAdapter(base_model="mlx-community/test")
        cmd = a.build_cmd("hello")
        assert "--model" in cmd
        assert cmd[cmd.index("--model") + 1] == "mlx-community/test"

    def test_adapter_path_included_when_exists(self, tmp_path: Path) -> None:
        adapter = tmp_path / "adapter"
        adapter.mkdir()
        a = MLXLocalAdapter(base_model="m", adapter_path=adapter)
        cmd = a.build_cmd("hello")
        assert "--adapter-path" in cmd
        assert cmd[cmd.index("--adapter-path") + 1] == str(adapter)

    def test_adapter_path_omitted_when_missing(self, tmp_path: Path) -> None:
        a = MLXLocalAdapter(base_model="m", adapter_path=tmp_path / "no_such")
        cmd = a.build_cmd("hello")
        assert "--adapter-path" not in cmd

    def test_max_tokens_in_cmd(self) -> None:
        a = MLXLocalAdapter(base_model="m", max_tokens=256)
        cmd = a.build_cmd("hello")
        assert "--max-tokens" in cmd
        assert cmd[cmd.index("--max-tokens") + 1] == "256"


# ---------------------------------------------------------------------------
# MLXLocalAdapter._parse_output
# ---------------------------------------------------------------------------

class TestParseOutput:
    def test_strips_prompt_prefix(self) -> None:
        raw = "Prompt: some long input\nThe answer is 42."
        assert MLXLocalAdapter._parse_output(raw) == "The answer is 42."

    def test_passthrough_clean(self) -> None:
        assert MLXLocalAdapter._parse_output("Hello world") == "Hello world"

    def test_strips_separator_lines(self) -> None:
        raw = "======\nClean output\n======"
        assert MLXLocalAdapter._parse_output(raw) == "Clean output"

    def test_strips_whitespace(self) -> None:
        assert MLXLocalAdapter._parse_output("  hi  \n") == "hi"


# ---------------------------------------------------------------------------
# MLXLocalAdapter.stream
# ---------------------------------------------------------------------------

class TestStream:
    def _run(self, adapter: MLXLocalAdapter, msgs: list[dict]) -> list[StreamChunk]:
        async def _collect():
            chunks = []
            async for chunk in adapter.stream(msgs):
                chunks.append(chunk)
            return chunks
        return asyncio.run(_collect())

    def test_yields_text_on_success(self) -> None:
        a = MLXLocalAdapter(runner=_ok_runner("Hello from local model"))
        chunks = self._run(a, [{"role": "user", "content": "Hi"}])
        assert len(chunks) == 1
        assert "Hello from local model" in chunks[0].delta_text

    def test_finish_reason_stop(self) -> None:
        a = MLXLocalAdapter(runner=_ok_runner("text"))
        chunks = self._run(a, [{"role": "user", "content": "q"}])
        assert chunks[0].finish_reason == "stop"

    def test_usage_set_on_success(self) -> None:
        a = MLXLocalAdapter(runner=_ok_runner("word " * 100))
        chunks = self._run(a, [{"role": "user", "content": "q"}])
        assert chunks[0].usage is not None
        assert chunks[0].usage["output_tokens"] >= 1

    def test_failure_yields_error_chunk(self) -> None:
        a = MLXLocalAdapter(runner=_fail_runner)
        chunks = self._run(a, [{"role": "user", "content": "q"}])
        assert len(chunks) == 1
        assert "mlx_local error" in chunks[0].delta_text
        assert chunks[0].finish_reason == "stop"

    def test_no_tool_call_delta(self) -> None:
        a = MLXLocalAdapter(runner=_ok_runner("answer"))
        chunks = self._run(a, [{"role": "user", "content": "q"}])
        assert all(c.tool_call_delta is None for c in chunks)


# ---------------------------------------------------------------------------
# Router integration — build_llm recognises mlx_local
# ---------------------------------------------------------------------------

class TestRouterIntegration:
    def test_build_llm_mlx_local(self) -> None:
        def spy(cmd):
            return 0, "response", ""

        adapter = build_llm({
            "provider": "mlx_local",
            "model": "mlx-community/test-model",
        })
        assert adapter.name == "mlx_local"

    def test_unknown_provider_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown provider"):
            build_llm({"provider": "mystery_llm", "model": "x"})


# ---------------------------------------------------------------------------
# THE VERIFICATION: zero-API-call summarise turn
# ---------------------------------------------------------------------------

class TestZeroAPICallVerification:
    def test_mlx_local_records_zero_cost(self, tmp_path: Path) -> None:
        """Phase gate: mlx_local turn → router_stats cost_usd=0.0, provider=mlx_local."""
        db = tmp_path / "stats.db"

        record_call(
            provider="mlx_local",
            model="mlx-community/Mistral-7B-Instruct-v0.2-4bit",
            task_kind="summarise",
            latency_ms=120,
            input_tokens=200,
            output_tokens=80,
            success=True,
            _db=db,
        )

        rows = p50_table(_db=db)
        assert len(rows) == 1
        row = rows[0]
        assert row["provider"] == "mlx_local"
        assert row["avg_cost_usd"] == 0.0, (
            f"mlx_local must record zero cost, got {row['avg_cost_usd']}"
        )
        assert row["task_kind"] == "summarise"

    def test_stream_produces_no_http_call(self) -> None:
        """mlx_local.stream completes without touching the network."""
        import socket
        original_connect = socket.socket.connect
        network_calls: list[tuple] = []

        def tracking_connect(self_sock, address):
            network_calls.append(address)
            return original_connect(self_sock, address)

        socket.socket.connect = tracking_connect
        try:
            adapter = MLXLocalAdapter(runner=_ok_runner("summary: done"))
            asyncio.run(adapter.stream(
                [{"role": "user", "content": "Summarise this document."}]
            ).__anext__())
        finally:
            socket.socket.connect = original_connect

        assert network_calls == [], (
            f"mlx_local must make zero network calls, got: {network_calls}"
        )
