"""Tool base types. Port of openhuman/src/openhuman/tools/traits.rs."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Awaitable, Callable


class Permission(IntEnum):
    """Permission tier. Higher = riskier. DANGEROUS requires approval gate."""

    NONE = 0
    READ_ONLY = 1
    WRITE = 2
    EXECUTE = 3
    DANGEROUS = 4

    @classmethod
    def parse(cls, value: "str | int | Permission") -> "Permission":
        if isinstance(value, cls):
            return value
        if isinstance(value, int):
            return cls(value)
        name = str(value).strip().upper()
        if name not in cls.__members__:
            raise ValueError(f"Unknown permission tier: {value!r}")
        return cls[name]


class ToolScope(IntEnum):
    SYSTEM = 0
    SKILL = 1
    INTEGRATION = 2


ToolHandler = Callable[[dict[str, Any], "ToolContext"], Awaitable[str]]


@dataclass
class ToolContext:
    """Passed to every tool handler. Carries session ref + workspace root."""

    session_id: str
    workspace: str
    config: dict[str, Any] = field(default_factory=dict)


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    permission: Permission
    scope: ToolScope
    handler: ToolHandler

    def to_openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def to_anthropic_schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.parameters,
        }


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ToolResult:
    call_id: str
    name: str
    content: str
    error: bool = False
