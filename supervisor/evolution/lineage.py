"""Ancestry reconstruction from the ledger."""

from __future__ import annotations

from typing import Any

from storage.events import FarmState


def ancestry(state: FarmState, agent_id: str) -> list[str]:
    """Agent first, then parent, grandparent, ... up to a seed or immigrant."""
    chain, seen = [], set()
    current: str | None = agent_id
    while current is not None and current not in seen:
        seen.add(current)
        chain.append(current)
        agent = state.agents.get(current)
        current = agent.parent_id if agent else None
    return chain


def lineage_tree(state: FarmState) -> dict[str, Any]:
    nodes = []
    for a in sorted(state.agents.values(), key=lambda a: (a.generation_born, a.id)):
        latest = {}
        for gen in sorted(state.fitness, reverse=True):
            if a.id in state.fitness[gen]:
                latest = state.fitness[gen][a.id]
                break
        nodes.append({
            "id": a.id, "parent_id": a.parent_id, "lineage_id": a.lineage_id, "origin": a.origin,
            "generation_born": a.generation_born, "retired_generation": a.retired_generation,
            "status": a.status, "status_reason": a.status_reason, "mutations": a.mutations,
            "segment": a.genotype["target"]["segment"], "price": a.genotype["pricing_parameters"]["price"],
            "fitness": latest.get("fitness"), "net_realized": latest.get("net_realized"),
        })
    edges = [{"from": n["parent_id"], "to": n["id"]} for n in nodes if n["parent_id"]]
    return {"nodes": nodes, "edges": edges}
