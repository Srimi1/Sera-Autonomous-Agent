"""Provider/profile router."""
from __future__ import annotations

from typing import Any

from sera.llm.base import LLM


def build_llm(profile_cfg: dict[str, Any]) -> LLM:
    provider = profile_cfg["provider"]
    model = profile_cfg["model"]
    if provider == "openai":
        from sera.llm.adapters.openai_adapter import OpenAIAdapter

        return OpenAIAdapter(model=model)
    if provider == "anthropic":
        from sera.llm.adapters.anthropic_adapter import AnthropicAdapter

        return AnthropicAdapter(model=model)
    raise ValueError(f"Unknown provider: {provider}")


def for_profile(config: dict[str, Any], profile: str | None = None) -> LLM:
    profile = profile or config["llm"]["default_profile"]
    cfg = config["llm"]["profiles"][profile]
    return build_llm(cfg)
