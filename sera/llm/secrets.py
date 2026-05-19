"""API key resolution: env var → keyring."""
from __future__ import annotations

import os

SERVICE = "sera"


def get_key(provider: str) -> str | None:
    """Provider names: 'openai', 'anthropic'. Env wins, then keyring."""
    env_name = {
        "openai": "OPENAI_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
    }.get(provider)
    if env_name and (val := os.environ.get(env_name)):
        return val
    try:
        import keyring

        return keyring.get_password(SERVICE, f"{provider}_api_key")
    except Exception:  # noqa: BLE001 — keyring absence is OK in headless tests
        return None


def set_key(provider: str, key: str) -> None:
    import keyring

    keyring.set_password(SERVICE, f"{provider}_api_key", key)
