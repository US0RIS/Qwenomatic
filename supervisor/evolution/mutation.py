"""Typed, bounded, diffable mutation (DESIGN §10.3).

Operators may only target genotype paths in MUTABLE_PATHS. Supervisor code,
policy, credentials, accounting, fitness, reproduction and spending ceilings
are not part of the genotype and cannot be registered as mutation targets.
"""

from __future__ import annotations

import math
import random
from dataclasses import asdict, dataclass
from typing import Any, Callable

from runtime.agent.model import MUTABLE_PATHS, clone_genotype, get_path, set_path, validate_genotype


class MutationError(Exception):
    pass


@dataclass(frozen=True)
class MutationDiff:
    type: str
    path: str
    before: Any
    after: Any

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


Apply = Callable[[Any, random.Random, dict[str, Any]], Any]


@dataclass(frozen=True)
class MutationOperator:
    type: str
    path: str
    apply: Apply


class MutationRegistry:
    def __init__(self) -> None:
        self._ops: dict[str, MutationOperator] = {}

    def register(self, op: MutationOperator) -> None:
        if op.path not in MUTABLE_PATHS:
            raise MutationError(f"path {op.path!r} is not mutable")
        self._ops[op.type] = op

    def operators(self) -> list[MutationOperator]:
        return [self._ops[k] for k in sorted(self._ops)]


# ------------------------------------------------------------- operators
def _price(before: float, rng: random.Random, cfg: dict[str, Any]) -> float:
    b = cfg["price"]
    return round(min(b["max"], max(b["min"], before * math.exp(rng.gauss(0.0, b["sigma"])))), 2)


def _segment(before: str, rng: random.Random, cfg: dict[str, Any]) -> str:
    options = [s for s in cfg["_segments"] if s != before]
    return rng.choice(options) if options else before


def _workflow(before: str, rng: random.Random, cfg: dict[str, Any]) -> str:
    options = [w for w in cfg["workflows"] if w != before]
    return rng.choice(options) if options else before


def _prompt(before: str, rng: random.Random, cfg: dict[str, Any]) -> str:
    bank = cfg["strategy_fragments"]
    present = [f for f in bank if f in before]
    absent = [f for f in bank if f not in before]
    residue = before
    for f in present:
        residue = residue.replace(f, "")
    residue = " ".join(residue.split())
    parts = list(present)
    op = rng.random()
    if parts and absent and op < 0.5:
        parts[rng.randrange(len(parts))] = rng.choice(absent)
    elif absent and (op < 0.8 or len(parts) <= 1) and len(parts) < 4:
        parts.append(rng.choice(absent))
    elif len(parts) > 1:
        parts.pop(rng.randrange(len(parts)))
    out = " ".join(([residue] if residue else []) + parts)
    return out[: int(cfg.get("max_prompt_chars", 1200))]


def _tools(before: list[str], rng: random.Random, cfg: dict[str, Any]) -> list[str]:
    required = set(cfg.get("required_tools", []))
    optional = [t for t in cfg["tools"] if t not in required]
    if not optional:
        return list(before)
    t = rng.choice(optional)
    out = set(before)
    out.symmetric_difference_update({t})
    return sorted(out | required)


def _temperature(before: float, rng: random.Random, cfg: dict[str, Any]) -> float:
    b = cfg["temperature"]
    return round(min(b["max"], max(b["min"], before + rng.gauss(0.0, b["sigma"]))), 2)


def _survey_every(before: int, rng: random.Random, cfg: dict[str, Any]) -> int:
    b = cfg["survey_every"]
    return int(min(b["max"], max(b["min"], before + rng.choice([-2, -1, 1, 2]))))


def default_registry() -> MutationRegistry:
    reg = MutationRegistry()
    for op in (
        MutationOperator("pricing_parameter", "pricing_parameters.price", _price),
        MutationOperator("target_segment", "target.segment", _segment),
        MutationOperator("workflow", "workflow", _workflow),
        MutationOperator("strategy_prompt", "strategy_prompt", _prompt),
        MutationOperator("tool_preferences", "tool_preferences", _tools),
        MutationOperator("planning_temperature", "planning_parameters.temperature", _temperature),
        MutationOperator("planning_survey_every", "planning_parameters.survey_every", _survey_every),
    ):
        reg.register(op)
    return reg


# Relative frequency of each mutation type. Pricing and segment moves are the
# main economic levers; the rest adjust behaviour.
DEFAULT_WEIGHTS = {
    "pricing_parameter": 4.0, "target_segment": 1.5, "workflow": 1.0, "strategy_prompt": 1.0,
    "tool_preferences": 0.5, "planning_temperature": 0.75, "planning_survey_every": 0.75,
}


class MutationEngine:
    def __init__(self, cfg: dict[str, Any], *, segments: list[str], granted_tools: list[str],
                 max_mutations: int = 2, registry: MutationRegistry | None = None) -> None:
        self.cfg = {**cfg, "_segments": list(segments)}
        self.segments = list(segments)
        self.granted_tools = list(granted_tools)
        self.max_mutations = max(1, int(max_mutations))
        self.registry = registry or default_registry()

    def validate(self, genotype: Any) -> list[str]:
        return validate_genotype(genotype, mutation_cfg=self.cfg, segments=self.segments,
                                 granted_tools=self.granted_tools)

    def mutate(self, genotype: dict[str, Any], rng: random.Random) -> tuple[dict[str, Any], list[MutationDiff]]:
        child = clone_genotype(genotype)
        ops = self.registry.operators()
        weights = [DEFAULT_WEIGHTS.get(op.type, 1.0) for op in ops]
        n = rng.randint(1, self.max_mutations)
        diffs: list[MutationDiff] = []
        attempts = 0
        while len(diffs) < n and attempts < 20:
            attempts += 1
            op = rng.choices(ops, weights=weights)[0]
            if any(d.path == op.path for d in diffs):
                continue
            before = get_path(child, op.path)
            after = op.apply(before if not isinstance(before, list) else list(before), rng, self.cfg)
            if after == before:
                continue
            set_path(child, op.path, after)
            diffs.append(MutationDiff(op.type, op.path, before, after))
        errors = self.validate(child)
        if errors:
            raise MutationError("; ".join(errors))
        return child, diffs
