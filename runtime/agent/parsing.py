"""Parse model output into typed action requests.

Model output is untrusted data. Anything that does not parse into the
expected structure is a malformed output (a health signal), never an action.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


class MalformedOutput(Exception):
    pass


@dataclass
class ParsedOutput:
    actions: list[dict[str, Any]]
    memory: str | None = None
    thought: str | None = None
    claims: dict[str, Any] | None = None
    suggestion: str | None = None
    dropped_actions: int = 0
    raw_keys: list[str] = field(default_factory=list)


def _strict_json(text: str) -> Any:
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise MalformedOutput("duplicate JSON key")
            out[key] = value
        return out
    def constant(value):
        raise MalformedOutput("non-finite JSON number")
    return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)


def _extract_json(text: str) -> Any:
    if not isinstance(text, str) or len(text) > 1_000_000:
        raise MalformedOutput("model output exceeds limit or is not text")
    text = _THINK.sub("", text).strip()
    try:
        return _strict_json(text)
    except json.JSONDecodeError:
        pass
    m = _FENCE.search(text)
    if m:
        try:
            return _strict_json(m.group(1))
        except json.JSONDecodeError:
            pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return _strict_json(text[start : end + 1])
        except json.JSONDecodeError:
            pass
    raise MalformedOutput("no JSON object found")


def parse_output(text: str, *, max_actions: int) -> ParsedOutput:
    data = _extract_json(text or "")
    if not isinstance(data, dict):
        raise MalformedOutput("top-level value must be an object")
    actions = data.get("actions", [])
    if not isinstance(actions, list):
        raise MalformedOutput("actions must be a list")
    clean = []
    for a in actions:
        if not isinstance(a, dict) or not isinstance(a.get("tool"), str):
            raise MalformedOutput("each action needs a string 'tool'")
        args = a.get("args", {})
        if not isinstance(args, dict):
            raise MalformedOutput("action args must be an object")
        clean.append({"tool": a["tool"][:100], "args": args})
    claims = data.get("claims")
    # Numbers the model asserts about its own performance are recorded as
    # claims and ignored by accounting.
    for key in ("revenue", "profit", "sales", "earnings"):
        if key in data:
            claims = {**(claims or {}), key: data[key]}
    memory = data.get("memory")
    thought = data.get("thought")
    suggestion = data.get("strategy_suggestion")
    return ParsedOutput(
        actions=clean[:max_actions],
        memory=str(memory)[:500] if memory is not None else None,
        thought=str(thought)[:500] if thought is not None else None,
        claims=claims if isinstance(claims, dict) else ({"text": str(claims)[:500]} if claims else None),
        suggestion=str(suggestion)[:1000] if suggestion else None,
        dropped_actions=max(0, len(clean) - max_actions),
        raw_keys=sorted(data.keys()),
    )

