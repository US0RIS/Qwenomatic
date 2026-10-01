"""Agent and genotype representation (DESIGN §5).

An agent is a logical entity: persistent state plus an inheritable genotype,
served by shared inference. The genotype holds only strategy. Budgets,
permissions, scoring and reproduction live with the supervisor and are not
representable here, so mutation cannot reach them (DESIGN §10.3).
"""

from __future__ import annotations

import copy
import math
import random
from dataclasses import dataclass, field
from typing import Any

from storage.events.canonical import digest

GENOTYPE_VERSION = 1

GENOTYPE_SCHEMA: dict[str, Any] = {
    "strategy_prompt": str,
    "workflow": str,
    "target": {"segment": str},
    "pricing_parameters": {"price": (int, float)},
    "tool_preferences": list,
    "planning_parameters": {"temperature": (int, float), "max_tokens": int, "survey_every": int},
}

# Leaf paths the mutation engine may touch. Nothing else is mutable.
MUTABLE_PATHS = frozenset({
    "strategy_prompt",
    "workflow",
    "target.segment",
    "pricing_parameters.price",
    "tool_preferences",
    "planning_parameters.temperature",
    "planning_parameters.survey_every",
})

STATUSES = ("queued", "running", "paused", "retired", "disqualified")


@dataclass
class Agent:
    id: str
    lineage_id: str
    parent_id: str | None
    generation: int
    genotype_version: int
    genotype: dict[str, Any]
    phenotype_state: dict[str, Any] = field(default_factory=dict)
    budgets: dict[str, Any] = field(default_factory=dict)
    status: str = "queued"


def genotype_digest(genotype: dict[str, Any]) -> str:
    return digest(genotype)


def validate_genotype(
    genotype: Any,
    *,
    mutation_cfg: dict[str, Any],
    segments: list[str],
    granted_tools: list[str],
) -> list[str]:
    errors: list[str] = []
    if not isinstance(genotype, dict):
        return ["genotype must be a mapping"]
    _check_schema(genotype, GENOTYPE_SCHEMA, "", errors)
    if errors:
        return errors
    if len(genotype["strategy_prompt"]) > int(mutation_cfg.get("max_prompt_chars", 2000)):
        errors.append("strategy_prompt too long")
    if genotype["workflow"] not in mutation_cfg["workflows"]:
        errors.append(f"workflow {genotype['workflow']!r} not permitted")
    if genotype["target"]["segment"] not in segments:
        errors.append(f"segment {genotype['target']['segment']!r} not approved")
    price = float(genotype["pricing_parameters"]["price"])
    pb = mutation_cfg["price"]
    if not (math.isfinite(price) and pb["min"] <= price <= pb["max"]):
        errors.append("price out of bounds")
    tools = genotype["tool_preferences"]
    if not all(isinstance(t, str) for t in tools):
        errors.append("tool_preferences must be strings")
    else:
        if not set(tools) <= set(granted_tools) or not set(tools) <= set(mutation_cfg["tools"]):
            errors.append("tool_preferences exceed granted capabilities")
        for req in mutation_cfg.get("required_tools", []):
            if req not in tools:
                errors.append(f"required tool {req} missing")
    pp = genotype["planning_parameters"]
    tb, sb = mutation_cfg["temperature"], mutation_cfg["survey_every"]
    if not tb["min"] <= float(pp["temperature"]) <= tb["max"]:
        errors.append("temperature out of bounds")
    if not sb["min"] <= int(pp["survey_every"]) <= sb["max"]:
        errors.append("survey_every out of bounds")
    if not 64 <= int(pp["max_tokens"]) <= 4096:
        errors.append("max_tokens out of bounds")
    return errors


def _check_schema(value: Any, schema: Any, path: str, errors: list[str]) -> None:
    if isinstance(schema, dict):
        if not isinstance(value, dict):
            errors.append(f"{path or 'genotype'} must be a mapping")
            return
        extra = set(value) - set(schema)
        missing = set(schema) - set(value)
        for k in sorted(extra):
            errors.append(f"unknown genotype field {path + k!r}")
        for k in sorted(missing):
            errors.append(f"missing genotype field {path + k!r}")
        for k in set(schema) & set(value):
            _check_schema(value[k], schema[k], f"{path}{k}.", errors)
    elif isinstance(value, bool) or not isinstance(value, schema):
        errors.append(f"genotype field {path.rstrip('.')!r} has wrong type")


def get_path(genotype: dict[str, Any], path: str) -> Any:
    node: Any = genotype
    for part in path.split("."):
        node = node[part]
    return node


def set_path(genotype: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    node = genotype
    for part in parts[:-1]:
        node = node[part]
    node[parts[-1]] = value


def random_genotype(rng: random.Random, mutation_cfg: dict[str, Any], segments: list[str]) -> dict[str, Any]:
    fragments = mutation_cfg["strategy_fragments"]
    pb = mutation_cfg["price"]
    tb = mutation_cfg["temperature"]
    sb = mutation_cfg["survey_every"]
    optional = [t for t in mutation_cfg["tools"] if t not in mutation_cfg.get("required_tools", [])]
    tools = list(mutation_cfg.get("required_tools", [])) + [t for t in optional if rng.random() < 0.6]
    price = math.exp(rng.uniform(math.log(max(pb["min"], 5.0)), math.log(min(pb["max"], 300.0))))
    return {
        "strategy_prompt": " ".join(rng.sample(fragments, k=min(2, len(fragments)))),
        "workflow": rng.choice(mutation_cfg["workflows"]),
        "target": {"segment": rng.choice(segments)},
        "pricing_parameters": {"price": round(price, 2)},
        "tool_preferences": sorted(set(tools)),
        "planning_parameters": {
            "temperature": round(rng.uniform(tb["min"], tb["max"]), 2),
            "max_tokens": 512,
            "survey_every": rng.randint(sb["min"], sb["max"]),
        },
    }


def clone_genotype(genotype: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(genotype)
