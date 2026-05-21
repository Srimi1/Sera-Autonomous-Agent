"""Tool registry. Heritage: hermes/tools/registry.py — auto-discovery + self-register."""
from __future__ import annotations

import importlib
import pkgutil
from typing import Iterable

from sera.tools.base import Permission, Tool

_registry: dict[str, Tool] = {}
_discovered: bool = False


def register(tool: Tool) -> None:
    # Idempotent: last registration wins. Lets test harness rediscover cleanly.
    _registry[tool.name] = tool


def unregister(name: str) -> bool:
    """Drop a tool by name. Returns True iff a tool was removed."""
    return _registry.pop(name, None) is not None


def get(name: str) -> Tool | None:
    _ensure_discovered()
    return _registry.get(name)


def all_tools(max_permission: Permission = Permission.DANGEROUS) -> list[Tool]:
    _ensure_discovered()
    return [t for t in _registry.values() if t.permission <= max_permission]


def names() -> Iterable[str]:
    _ensure_discovered()
    return _registry.keys()


def _ensure_discovered() -> None:
    global _discovered
    if _discovered:
        return
    import sera.tools.impl as impl_pkg

    for mod in pkgutil.iter_modules(impl_pkg.__path__, prefix=f"{impl_pkg.__name__}."):
        importlib.import_module(mod.name)
    _discovered = True


def reset() -> None:
    """Test helper: drop cached impl modules so the next access re-executes registrations."""
    import sys

    global _discovered
    _registry.clear()
    _discovered = False
    for name in list(sys.modules):
        if name.startswith("sera.tools.impl."):
            del sys.modules[name]
