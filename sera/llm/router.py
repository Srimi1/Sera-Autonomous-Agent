"""Provider/profile router."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from sera.llm.base import LLM


def build_llm(profile_cfg: dict[str, Any]) -> LLM:
    provider = profile_cfg["provider"]
    model = profile_cfg.get("model", "")
    if provider == "openai":
        from sera.llm.adapters.openai_adapter import OpenAIAdapter

        return OpenAIAdapter(model=model)
    if provider == "anthropic":
        from sera.llm.adapters.anthropic_adapter import AnthropicAdapter

        return AnthropicAdapter(model=model)
    if provider == "mlx_local":
        from sera.llm.adapters.mlx_local import MLXLocalAdapter

        adapter_path = profile_cfg.get("adapter_path")
        max_tokens = int(profile_cfg.get("max_tokens", 512))
        return MLXLocalAdapter(
            base_model=model or "mlx-community/Mistral-7B-Instruct-v0.2-4bit",
            adapter_path=Path(adapter_path) if adapter_path else None,
            max_tokens=max_tokens,
        )
    if provider == "llama_cpp":
        from sera.llm.adapters.llama_cpp import LlamaCppAdapter

        model_path = profile_cfg.get("model_path")
        max_tokens = int(profile_cfg.get("max_tokens", 512))
        n_ctx = int(profile_cfg.get("n_ctx", 4096))
        return LlamaCppAdapter(
            model_path=Path(model_path) if model_path else None,
            max_tokens=max_tokens,
            n_ctx=n_ctx,
        )
    raise ValueError(f"Unknown provider: {provider}")


def for_profile(config: dict[str, Any], profile: str | None = None) -> LLM:
    profile = profile or config["llm"]["default_profile"]
    cfg = config["llm"]["profiles"][profile]
    return build_llm(cfg)
