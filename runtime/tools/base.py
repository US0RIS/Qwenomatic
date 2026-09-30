"""Typed tool adapters. Agents receive narrow capabilities, never a shell."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ToolContext:
    agent_id: str
    lineage_id: str
    generation_id: int
    step_id: str | None
    invocation_id: str
    tick: int
    now: datetime
    workspace: Path
    max_spend: float | None = None


@dataclass
class ToolResult:
    ok: bool
    status: str  # ok | error | denied | pending_approval
    output: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def observation(self) -> dict[str, Any]:
        """What the agent is allowed to see about the outcome."""
        out: dict[str, Any] = {"status": self.status}
        if self.output:
            out["result"] = {k: v for k, v in self.output.items() if not k.startswith("_")}
        if self.error:
            out["error"] = self.error
        return out


class ToolError(Exception):
    pass


_TYPES = {"str": str, "number": (int, float), "int": int, "bool": bool}


class ToolAdapter:
    name: str = "tool"
    description: str = ""
    action_class: str = "unknown"
    args_schema: dict[str, str] = {}
    optional_args: frozenset[str] = frozenset()
    supports_spend_limit: bool = False

    def classify(self, args: dict[str, Any]) -> str:
        return self.action_class

    def spend(self, args: dict[str, Any]) -> float:
        return 0.0

    def validate(self, args: dict[str, Any]) -> list[str]:
        errors = []
        if not isinstance(args, dict):
            return ["args must be an object"]
        for key, typ in self.args_schema.items():
            if key not in args:
                if key not in self.optional_args:
                    errors.append(f"missing argument {key!r}")
                continue
            value = args[key]
            if isinstance(value, bool) and typ != "bool":
                errors.append(f"argument {key!r} must be {typ}")
            elif not isinstance(value, _TYPES[typ]):
                errors.append(f"argument {key!r} must be {typ}")
        for key in args:
            if key not in self.args_schema:
                errors.append(f"unexpected argument {key!r}")
        return errors

    def invoke(self, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        raise NotImplementedError

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "args": {k: (v + ("?" if k in self.optional_args else "")) for k, v in self.args_schema.items()},
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolAdapter] = {}

    def register(self, tool: ToolAdapter) -> None:
        if tool.name in self._tools:
            raise ValueError(f"tool {tool.name!r} already registered")
        self._tools[tool.name] = tool

    def get(self, name: str) -> ToolAdapter | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def describe(self, names: list[str]) -> list[dict[str, Any]]:
        return [self._tools[n].describe() for n in names if n in self._tools]
