"""Agent runtime: turns one inference completion into gated tool calls.

The runtime is trusted code hosting an untrusted policy (the model). The
model's only channel to the world is the JSON it emits; every action in it
goes through the supervisor's capability gateway.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from storage.events import EventStore, EventType, agent_author, digest

from ..inference.base import InferenceRequest
from ..tools.base import ToolRegistry
from .parsing import MalformedOutput, parse_output
from .prompts import build_messages


@dataclass
class StepOutcome:
    malformed: bool
    actions: int
    denied: int
    state_version: int
    state_digest: str
    error: str | None = None


class AgentRuntime:
    def __init__(self, *, store: EventStore, registry: ToolRegistry, gateway: Any, config: dict[str, Any]) -> None:
        self.store = store
        self.registry = registry
        self.gateway = gateway
        self.max_actions = int(config.get("max_actions_per_step", 3))
        self.memory_items = int(config.get("memory_items", 12))

    def load_state(self, agent_id: str) -> tuple[int, dict[str, Any], bool]:
        """Returns (version, state, recovered). Corrupt state is replaced, never trusted."""
        version, state = self.store.load_agent_state(agent_id)
        recovered = bool(state.pop("_corrupted", False))
        if not isinstance(state.get("step_index", 0), int):
            state["step_index"], recovered = 0, True
        for key in ("memory", "notes"):
            if not isinstance(state.get(key, []), list):
                state[key], recovered = [], True
        state.setdefault("step_index", 0)
        state.setdefault("memory", [])
        state.setdefault("notes", [])
        return version, state, recovered

    def build_request(self, agent: Any, state: dict[str, Any], seed: int) -> InferenceRequest:
        g = agent.genotype
        tools = self.registry.describe(g["tool_preferences"])
        messages = build_messages(g, tools, state["memory"], state["notes"], state["step_index"], self.max_actions)
        request_seed = int(digest([seed, agent.id, state["step_index"], "inference"])[:8], 16)
        return InferenceRequest(
            messages=messages,
            max_tokens=int(g["planning_parameters"]["max_tokens"]),
            temperature=float(g["planning_parameters"]["temperature"]),
            metadata={"agent_id": agent.id, "step_index": state["step_index"], "genotype": g,
                      "memory": state["memory"], "seed": request_seed},
        )

    def execute(self, agent: Any, token: str, step: Any, text: str, version: int, state: dict[str, Any]) -> StepOutcome:
        state = {**state, "memory": list(state["memory"]), "notes": list(state["notes"])}
        state["step_index"] += 1
        error: str | None = None
        actions = denied = 0
        try:
            parsed = parse_output(text, max_actions=self.max_actions)
        except MalformedOutput as exc:
            parsed = None
            error = str(exc)
        if parsed is not None:
            if parsed.claims:
                self.store.append(
                    EventType.AGENT_CLAIM, {"claims": parsed.claims, "step_id": step.step_id,
                                            "effect": "none: claims are not economic evidence"},
                    author=agent_author(agent.id), agent_id=agent.id, lineage_id=agent.lineage_id,
                    generation_id=step.generation_id,
                )
            if parsed.suggestion:
                self.store.append(
                    EventType.STRATEGY_SUGGESTION, {"text": parsed.suggestion, "step_id": step.step_id},
                    author=agent_author(agent.id), agent_id=agent.id, lineage_id=agent.lineage_id,
                    generation_id=step.generation_id,
                )
            for action in parsed.actions:
                result = self.gateway.invoke(token, action["tool"], action["args"], step)
                actions += 1
                if result.status == "denied":
                    denied += 1
                state["memory"].append({"tool": action["tool"], "args": _short(action["args"]), **result.observation()})
                if result.status == "denied" and agent.id and self._agent_stopped(agent.id):
                    break  # disqualified mid-step: nothing further executes
            if parsed.memory:
                state["notes"].append(parsed.memory)
        state["memory"] = state["memory"][-self.memory_items:]
        state["notes"] = state["notes"][-self.memory_items:]
        new_version = self.store.save_agent_state(agent.id, state, version)
        return StepOutcome(
            malformed=parsed is None, actions=actions, denied=denied, state_version=new_version,
            state_digest=digest(state), error=error,
        )

    def _agent_stopped(self, agent_id: str) -> bool:
        view = self.gateway.state.agents.get(agent_id)
        return view is not None and view.status == "disqualified"


def _short(args: dict[str, Any]) -> dict[str, Any]:
    return {k: (v[:120] if isinstance(v, str) else v) for k, v in args.items() if not isinstance(v, (dict, list))}
