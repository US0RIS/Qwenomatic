"""Causal check on scheduler priority (DESIGN §9.3).

More compute can create more revenue, which earns more compute. A randomized
control cohort receives equal allocation each generation; comparing its
per-step efficiency with the priority-scheduled cohort estimates whether
priority produces incremental returns or merely amplifies early luck.
"""

from __future__ import annotations

from typing import Any

from storage.events import FarmState

from .stats import mean, spearman


def causal_report(state: FarmState, generation: int) -> dict[str, Any]:
    gen = state.generations[generation]
    rows = []
    for agent_id, c in state.counters.get(generation, {}).items():
        if c.steps == 0 or agent_id not in state.agents:
            continue
        if state.agents[agent_id].role != 'business':
            continue
        rows.append({
            "agent_id": agent_id,
            "cohort": gen.cohort.get(agent_id, "treatment"),
            "steps": c.steps,
            "scheduled_ticks": c.scheduled_ticks,
            "net_per_step": c.net_realized / c.steps,
        })
    control = [r for r in rows if r["cohort"] == "control"]
    treatment = [r for r in rows if r["cohort"] != "control"]

    def pooled(group: list[dict[str, Any]]) -> float | None:
        steps = sum(r["steps"] for r in group)
        return sum(r["net_per_step"] * r["steps"] for r in group) / steps if steps else None

    control_eff, treatment_eff = pooled(control), pooled(treatment)
    rho = spearman([r["scheduled_ticks"] for r in treatment], [r["net_per_step"] for r in treatment])
    return {
        "generation": generation,
        "control_agents": len(control),
        "treatment_agents": len(treatment),
        "control_steps": sum(r["steps"] for r in control),
        "treatment_steps": sum(r["steps"] for r in treatment),
        "control_net_per_step": control_eff,
        "treatment_net_per_step": treatment_eff,
        "incremental_net_per_step": (treatment_eff - control_eff)
        if control_eff is not None and treatment_eff is not None else None,
        "allocation_efficiency_spearman": rho,
        "mean_control_allocation": mean(r["scheduled_ticks"] for r in control),
        "mean_treatment_allocation": mean(r["scheduled_ticks"] for r in treatment),
        "note": "Priority is producing incremental return when treatment efficiency exceeds control "
                "and allocation correlates positively with per-step efficiency.",
    }
