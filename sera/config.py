"""Config loader. Heritage: hermes/gateway/config.py."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

SERA_HOME = Path(os.environ.get("SERA_HOME", Path.home() / ".sera")).expanduser()
CONFIG_PATH = SERA_HOME / "config.yaml"
SESSIONS_DB = SERA_HOME / "sessions.db"
MEMORY_DB = SERA_HOME / "memory.db"
SKILLS_DIR = SERA_HOME / "skills"
VAULT_DIR = SERA_HOME / "vault"

DEFAULT_CONFIG: dict[str, Any] = {
    "identity": {"name": "", "timezone": "Etc/UTC"},
    "llm": {
        "default_profile": "reasoning",
        "profiles": {
            "reasoning": {"provider": "anthropic", "model": "claude-sonnet-4-6"},
            "fast": {"provider": "openai", "model": "gpt-4o-mini"},
        },
    },
    "memory": {
        "autofetch_interval_seconds": 1200,
    },
    "safety": {
        # Tools at this tier or above prompt the approval gate.
        # DANGEROUS = only destructive shell. EXECUTE = also plain shell + python_eval.
        # Default to DANGEROUS so the agent feels responsive; raise locally if needed.
        "approval_required_at_or_above": "DANGEROUS",
        "max_iterations": 25,
    },
    "budget": {
        "session_soft_usd": 0.50,
        "session_hard_usd": 1.00,
        "day_soft_usd": 2.00,
        "day_hard_usd": 5.00,
        # skill_limits: {"task_kind": [soft_usd, hard_usd]}
        "skill_limits": {},
    },
}


def ensure_home() -> None:
    SERA_HOME.mkdir(parents=True, exist_ok=True)
    SKILLS_DIR.mkdir(exist_ok=True)
    VAULT_DIR.mkdir(exist_ok=True)


def load() -> dict[str, Any]:
    ensure_home()
    if not CONFIG_PATH.exists():
        save(DEFAULT_CONFIG)
        return DEFAULT_CONFIG
    with CONFIG_PATH.open("r") as f:
        return yaml.safe_load(f) or DEFAULT_CONFIG


def save(cfg: dict[str, Any]) -> None:
    ensure_home()
    with CONFIG_PATH.open("w") as f:
        yaml.safe_dump(cfg, f, sort_keys=False)
