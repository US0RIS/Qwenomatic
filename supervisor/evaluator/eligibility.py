"""Hard eligibility gate. Runs before, and independently of, economic scoring.

    if hard_constraint_failed:
        eligible = false
        fitness = DISQUALIFIED
"""

from __future__ import annotations

from dataclasses import dataclass, field

from storage.events import AgentCounters, AgentView


@dataclass(frozen=True)
class Eligibility:
    eligible: bool
    reasons: list[str] = field(default_factory=list)
    health_failed: bool = False


def evaluate_eligibility(agent: AgentView, window: AgentCounters) -> Eligibility:
    reasons = [f"hard policy violation: {v.get('action_class')} ({v.get('reason')})" for v in window.violations]
    if agent.status == "disqualified" and not reasons:
        reasons.append(f"disqualified: {agent.status_reason}")
    health_failed = agent.status == "paused" and (agent.status_reason or "").startswith("health")
    return Eligibility(eligible=not reasons, reasons=reasons, health_failed=health_failed)
