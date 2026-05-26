"""P-93: llama.cpp adapter — edge LLM by default for private tasks."""
from __future__ import annotations

import asyncio


from sera.llm.adapters.llama_cpp import (
    LlamaCppAdapter,
    _build_chatml_prompt,
    _parse_output,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_runner(stdout: str, rc: int = 0):
    def _run(cmd: list[str]) -> tuple[int, str, str]:
        return rc, stdout, ""
    return _run


def _adapter(stdout: str = "hello from local") -> LlamaCppAdapter:
    return LlamaCppAdapter(
        model_path="/tmp/fake.gguf",
        runner=_fake_runner(stdout),
    )


async def _stream_text(adapter: LlamaCppAdapter, messages: list[dict]) -> str:
    chunks = []
    async for chunk in adapter.stream(messages, system=None):
        if chunk.delta_text:
            chunks.append(chunk.delta_text)
    return "".join(chunks)


# ---------------------------------------------------------------------------
# ChatML prompt builder
# ---------------------------------------------------------------------------

def test_chatml_user_message():
    prompt = _build_chatml_prompt([{"role": "user", "content": "Hi"}])
    assert "<|im_start|>user\nHi\n<|im_end|>" in prompt
    assert prompt.endswith("<|im_start|>assistant\n")


def test_chatml_system_prefix():
    prompt = _build_chatml_prompt([], system="Be helpful.")
    assert "<|im_start|>system\nBe helpful.\n<|im_end|>" in prompt


def test_chatml_multi_turn():
    msgs = [
        {"role": "user", "content": "Q1"},
        {"role": "assistant", "content": "A1"},
        {"role": "user", "content": "Q2"},
    ]
    prompt = _build_chatml_prompt(msgs)
    assert "Q1" in prompt
    assert "A1" in prompt
    assert "Q2" in prompt
    assert prompt.endswith("<|im_start|>assistant\n")


def test_chatml_tool_result_folded():
    msgs = [{"role": "tool", "content": "result data"}]
    prompt = _build_chatml_prompt(msgs)
    assert "[tool result] result data" in prompt


# ---------------------------------------------------------------------------
# build_cmd
# ---------------------------------------------------------------------------

def test_build_cmd_contains_model_path():
    a = LlamaCppAdapter(model_path="/path/to/model.gguf")
    cmd = a.build_cmd("hello")
    assert "/path/to/model.gguf" in " ".join(cmd)


def test_build_cmd_contains_max_tokens():
    a = LlamaCppAdapter(model_path="/m.gguf", max_tokens=256)
    cmd = a.build_cmd("hello")
    assert "256" in " ".join(cmd)


def test_build_cmd_is_python():
    a = LlamaCppAdapter(model_path="/m.gguf")
    cmd = a.build_cmd("x")
    import sys
    assert cmd[0] == sys.executable


# ---------------------------------------------------------------------------
# Output parser
# ---------------------------------------------------------------------------

def test_parse_strips_llama_prefixes():
    raw = "llama_model_load: loading\nggml_init: done\nthe answer is 42"
    assert _parse_output(raw) == "the answer is 42"


def test_parse_strips_trailing_im_end():
    raw = "hello world<|im_end|>"
    assert _parse_output(raw) == "hello world"


def test_parse_empty_returns_empty():
    assert _parse_output("") == ""


def test_parse_clean_text_unchanged():
    raw = "the quick brown fox"
    assert _parse_output(raw) == "the quick brown fox"


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------

def test_stream_returns_text():
    a = _adapter("the answer is 42")
    text = asyncio.run(_stream_text(a, [{"role": "user", "content": "Q"}]))
    assert "42" in text


def test_stream_increments_calls():
    a = _adapter("ok")
    asyncio.run(_stream_text(a, [{"role": "user", "content": "ping"}]))
    assert a.calls == 1


def test_stream_finish_reason_stop():
    a = _adapter("done")
    chunks = []
    async def _collect():
        async for c in a.stream([{"role": "user", "content": "x"}]):
            chunks.append(c)
    asyncio.run(_collect())
    assert chunks[-1].finish_reason == "stop"


def test_stream_non_zero_rc_still_yields():
    a = LlamaCppAdapter(
        model_path="/m.gguf",
        runner=_fake_runner("partial output", rc=1),
    )
    text = asyncio.run(_stream_text(a, [{"role": "user", "content": "x"}]))
    assert "partial" in text


# ---------------------------------------------------------------------------
# Router integration
# ---------------------------------------------------------------------------

def test_router_builds_llama_cpp_adapter():
    from sera.llm.router import build_llm
    cfg = {"provider": "llama_cpp", "model_path": "/tmp/m.gguf", "max_tokens": 128}
    adapter = build_llm(cfg)
    from sera.llm.adapters.llama_cpp import LlamaCppAdapter
    assert isinstance(adapter, LlamaCppAdapter)


def test_router_default_model_path_when_none():
    from sera.llm.router import build_llm
    cfg = {"provider": "llama_cpp"}
    adapter = build_llm(cfg)
    assert "default.gguf" in str(adapter._model_path)
