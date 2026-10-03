"""Materialized views over the ledger.

`FarmState` is rebuilt by replaying events and then kept current by applying
each appended event. Nothing in it is authoritative: the ledger is. Any view
can be discarded and reconstructed (DESIGN §13, invariant I9).
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Iterable

from .types import Event

ACTIVE_STATUSES = ("queued", "running", "paused")


@dataclass
class AgentCounters:
    """Per-agent, per-generation counters. Reset implicitly each generation."""

    steps: int = 0
    malformed: int = 0
    consecutive_malformed: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    gpu_seconds: float = 0.0
    jobs_failed: int = 0
    tool_calls: int = 0
    denied: int = 0
    violations: list[dict[str, Any]] = field(default_factory=list)
    opportunities: int = 0
    conversions: int = 0
    gross_revenue: float = 0.0
    refunds: float = 0.0
    fees: float = 0.0
    external_spend: float = 0.0
    api_cloud_spend: float = 0.0
    compute_cost: float = 0.0
    other_costs: float = 0.0
    unrealized: float = 0.0
    milestone_value: float = 0.0
    human_interventions: int = 0
    claims: int = 0
    scheduled_ticks: int = 0
    step_net: dict[str, float] = field(default_factory=dict)

    @property
    def tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def total_costs(self) -> float:
        return self.fees + self.external_spend + self.api_cloud_spend + self.compute_cost + self.other_costs

    @property
    def net_realized(self) -> float:
        return self.gross_revenue - self.refunds - self.total_costs

    def merge(self, other: "AgentCounters") -> "AgentCounters":
        out = AgentCounters()
        for name in (
            "steps", "malformed", "prompt_tokens", "completion_tokens", "gpu_seconds", "jobs_failed",
            "tool_calls", "denied", "opportunities", "conversions", "gross_revenue", "refunds", "fees",
            "external_spend", "api_cloud_spend", "compute_cost", "other_costs", "unrealized",
            "milestone_value", "human_interventions", "claims", "scheduled_ticks",
        ):
            setattr(out, name, getattr(self, name) + getattr(other, name))
        out.consecutive_malformed = other.consecutive_malformed
        out.violations = self.violations + other.violations
        out.step_net = {**self.step_net, **other.step_net}
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "steps": self.steps, "malformed": self.malformed, "tokens": self.tokens,
            "gpu_seconds": round(self.gpu_seconds, 4), "jobs_failed": self.jobs_failed,
            "tool_calls": self.tool_calls, "denied": self.denied, "violations": len(self.violations),
            "opportunities": self.opportunities, "conversions": self.conversions,
            "gross_revenue": round(self.gross_revenue, 4), "refunds": round(self.refunds, 4),
            "fees": round(self.fees, 4), "external_spend": round(self.external_spend, 4),
            "api_cloud_spend": round(self.api_cloud_spend, 4), "compute_cost": round(self.compute_cost, 6),
            "other_costs": round(self.other_costs, 4), "unrealized": round(self.unrealized, 4),
            "net_realized": round(self.net_realized, 4), "milestone_value": round(self.milestone_value, 4),
            "human_interventions": self.human_interventions, "claims": self.claims,
            "scheduled_ticks": self.scheduled_ticks,
        }


@dataclass
class AgentView:
    id: str
    lineage_id: str
    parent_id: str | None
    generation_born: int
    genotype_version: int
    genotype: dict[str, Any]
    budgets: dict[str, Any]
    origin: str
    status: str
    status_reason: str | None = None
    retired_generation: int | None = None
    last_scheduled_tick: int | None = None
    burst: int = 0
    mutations: list[dict[str, Any]] = field(default_factory=list)
    role: str = "business"
    role_slot: int | None = None

    @property
    def active(self) -> bool:
        return self.status in ACTIVE_STATUSES


@dataclass
class GenerationView:
    number: int
    start_tick: int
    started_at: str
    ends_at: str
    config: dict[str, Any]
    cohort: dict[str, str]
    closing: bool = False
    closed: bool = False
    close_steps: dict[str, dict[str, Any]] = field(default_factory=dict)


class FarmState:
    def __init__(self, cap_window: int = 256) -> None:
        self.cap_window = cap_window
        self.reset()

    # ------------------------------------------------------------------ core
    def reset(self) -> None:
        self.last_seq = 0
        self.agents: dict[str, AgentView] = {}
        self.generations: dict[int, GenerationView] = {}
        self.current_generation: int | None = None
        self.counters: dict[int, dict[str, AgentCounters]] = defaultdict(lambda: defaultdict(AgentCounters))
        self.last_tick: int = -1
        self.allocations: deque[dict[str, Any]] = deque(maxlen=self.cap_window)
        self.gpu_by_tick: dict[int, float] = defaultdict(float)
        self.spend_by_tick: dict[int, float] = defaultdict(float)
        self.pending_settlements: dict[str, dict[str, Any]] = {}
        self.approvals: dict[str, dict[str, Any]] = {}
        self.outbound_pending: dict[str, Event] = {}
        self.capability_epoch: int = 0
        self.halted: bool = False
        self.halt_reason: str | None = None
        self.fitness: dict[int, dict[str, dict[str, Any]]] = defaultdict(dict)
        self.selections: dict[int, dict[str, Any]] = {}
        self.human_interventions: int = 0
        self.health_events: int = 0
        self.policy_decisions: dict[str, int] = defaultdict(int)
        self.violations_total: int = 0
        self.jobs_inflight: dict[str, dict[str, Any]] = {}
        self.role_layouts: dict[int, dict[str, Any]] = {}

    def replay(self, events: Iterable[Event]) -> "FarmState":
        for e in events:
            self.apply(e)
        return self

    def on_rollback(self) -> None:  # pragma: no cover - replaced by owner
        raise RuntimeError("FarmState needs its owner to rebuild after rollback")

    # --------------------------------------------------------------- queries
    def counter(self, generation: int, agent_id: str) -> AgentCounters:
        return self.counters[generation][agent_id]

    def active_agents(self) -> list[AgentView]:
        return [a for a in self.agents.values() if a.active]

    def lineage_members(self, lineage_id: str) -> list[AgentView]:
        return [a for a in self.agents.values() if a.lineage_id == lineage_id]

    def window_counters(self, agent_id: str, generations: Iterable[int]) -> AgentCounters:
        total = AgentCounters()
        for g in generations:
            if agent_id in self.counters.get(g, {}):
                total = total.merge(self.counters[g][agent_id])
        return total

    # ----------------------------------------------------------------- apply
    def apply(self, e: Event) -> None:
        self.last_seq = e.seq
        handler = getattr(self, f"_on_{e.type.value}", None)
        if handler:
            handler(e)

    def _c(self, e: Event) -> AgentCounters | None:
        if e.agent_id is None or e.generation_id is None:
            return None
        return self.counters[e.generation_id][e.agent_id]

    def _on_agent_created(self, e: Event) -> None:
        p = e.payload
        self.agents[e.agent_id] = AgentView(
            id=e.agent_id, lineage_id=e.lineage_id, parent_id=p.get("parent_id"),
            generation_born=p["generation"], genotype_version=p.get("genotype_version", 1),
            genotype=p["genotype"], budgets=p.get("budgets", {}), origin=p.get("origin", "seed"),
            status=p.get("status", "queued"), mutations=p.get("mutations", []),
            role=p.get("role", "business"), role_slot=p.get("role_slot"),
        )

    def _on_role_layout_planned(self, e: Event) -> None:
        self.role_layouts[e.generation_id] = e.payload['layout']

    def _on_agent_status_changed(self, e: Event) -> None:
        a = self.agents.get(e.agent_id)
        if a and a.status not in ("retired",):
            a.status = e.payload["status"]
            a.status_reason = e.payload.get("reason")

    def _on_agent_retired(self, e: Event) -> None:
        a = self.agents.get(e.agent_id)
        if a:
            a.status = "retired"
            a.status_reason = e.payload.get("reason")
            a.retired_generation = e.generation_id

    def _on_agent_step_completed(self, e: Event) -> None:
        c = self._c(e)
        if c is None:
            return
        c.steps += 1
        c.step_net.setdefault(e.payload["step_id"], 0.0)
        if e.payload.get("malformed"):
            c.malformed += 1
            c.consecutive_malformed += 1
        else:
            c.consecutive_malformed = 0

    def _on_agent_claim(self, e: Event) -> None:
        c = self._c(e)
        if c:
            c.claims += 1

    def _on_generation_started(self, e: Event) -> None:
        p = e.payload
        self.generations[p["number"]] = GenerationView(
            number=p["number"], start_tick=p["start_tick"], started_at=p["started_at"], ends_at=p["ends_at"],
            config=p["config"], cohort=p.get("cohort", {}),
        )
        self.current_generation = p["number"]
        for agent_id in p.get("population", []):
            a = self.agents.get(agent_id)
            if a and a.status == "queued":
                a.status = "running"

    def _on_generation_close_step(self, e: Event) -> None:
        g = self.generations[e.generation_id]
        g.closing = True
        g.close_steps[e.payload["step"]] = e.payload.get("data", {})

    def _on_generation_closed(self, e: Event) -> None:
        g = self.generations[e.generation_id]
        g.closing = False
        g.closed = True

    def _on_fitness_evaluated(self, e: Event) -> None:
        self.fitness[e.generation_id][e.agent_id] = e.payload

    def _on_selection_decided(self, e: Event) -> None:
        self.selections[e.generation_id] = e.payload

    def _on_mutation_applied(self, e: Event) -> None:
        pass

    def _on_inference_job_submitted(self, e: Event) -> None:
        self.jobs_inflight[e.payload["job_id"]] = {"agent_id": e.agent_id, "generation": e.generation_id, **e.payload}

    def _on_inference_job_completed(self, e: Event) -> None:
        self.jobs_inflight.pop(e.payload["job_id"], None)
        c = self._c(e)
        u = e.payload["usage"]
        if c:
            c.prompt_tokens += u["prompt_tokens"]
            c.completion_tokens += u["completion_tokens"]
            c.gpu_seconds += u["gpu_seconds"]
        self.gpu_by_tick[e.payload.get("tick", self.last_tick)] += u["gpu_seconds"]

    def _on_inference_job_failed(self, e: Event) -> None:
        self.jobs_inflight.pop(e.payload["job_id"], None)
        c = self._c(e)
        if c:
            c.jobs_failed += 1
            if e.payload.get('usage'):
                u = e.payload['usage']
                c.prompt_tokens += u['prompt_tokens']
                c.completion_tokens += u['completion_tokens']
                c.gpu_seconds += u['gpu_seconds']
                self.gpu_by_tick[e.payload.get('tick', self.last_tick)] += u['gpu_seconds']

    def _on_inference_job_cancelled(self, e: Event) -> None:
        self.jobs_inflight.pop(e.payload["job_id"], None)

    def _on_scheduler_allocation(self, e: Event) -> None:
        p = e.payload
        self.last_tick = max(self.last_tick, p["tick"])
        selected = [s["agent_id"] for s in p["selected"]]
        self.allocations.append(
            {"tick": p["tick"], "agents": selected, "lineages": [s["lineage_id"] for s in p["selected"]]}
        )
        chosen = set(selected)
        for agent_id in chosen:
            a = self.agents.get(agent_id)
            if a is None:
                continue
            a.burst = a.burst + 1 if a.last_scheduled_tick == p["tick"] - 1 else 1
            a.last_scheduled_tick = p["tick"]
            if e.generation_id is not None:
                self.counters[e.generation_id][agent_id].scheduled_ticks += 1

    def _on_capability_issued(self, e: Event) -> None:
        pass

    def _on_capabilities_revoked(self, e: Event) -> None:
        self.capability_epoch = max(self.capability_epoch, e.payload["epoch"])

    def _on_policy_decision(self, e: Event) -> None:
        self.policy_decisions[e.payload["decision"]] += 1
        c = self._c(e)
        if c and e.payload["decision"] == "deny":
            c.denied += 1
        approval_id = e.payload.get("approval_id")
        if approval_id in self.approvals and e.payload["decision"] == "deny":
            # An approved request refused by a later check is not retried.
            self.approvals[approval_id]["status"] = "failed"

    def _on_policy_violation(self, e: Event) -> None:
        self.violations_total += 1
        c = self._c(e)
        if c:
            c.violations.append(e.payload)

    def _on_tool_invoked(self, e: Event) -> None:
        c = self._c(e)
        if c:
            c.tool_calls += 1
        approval_id = e.payload.get("approval_id")
        if approval_id and approval_id in self.approvals:
            self.approvals[approval_id]["status"] = "executed"

    def _on_outbound_queued(self, e: Event) -> None:
        self.outbound_pending[e.payload["invocation_id"]] = e

    def _on_outbound_attempted(self, e: Event) -> None:
        self.outbound_pending.pop(e.payload["invocation_id"], None)

    def _on_human_approval_requested(self, e: Event) -> None:
        self.approvals[e.payload["approval_id"]] = {
            "approval_id": e.payload["approval_id"], "agent_id": e.agent_id, "generation": e.generation_id,
            "request": e.payload["request"], "reason": e.payload.get("reason"), "status": "pending",
            "requested_at": e.recorded_at,
        }

    def _on_human_approval_resolved(self, e: Event) -> None:
        a = self.approvals.get(e.payload["approval_id"])
        if a:
            a["status"] = "granted" if e.payload["granted"] else "denied"
            a["operator"] = e.payload.get("operator")

    def _on_human_intervention(self, e: Event) -> None:
        self.human_interventions += 1
        c = self._c(e)
        if c:
            c.human_interventions += 1

    def _on_opportunity(self, e: Event) -> None:
        c = self._c(e)
        if c:
            c.opportunities += 1

    def _on_financial_event(self, e: Event) -> None:
        p = e.payload
        amount = float(p["amount"])
        c = self._c(e)
        tick = p.get("tick", self.last_tick)
        if p.get("status") == "unrealized":
            if c:
                c.unrealized += amount if p["type"] == "revenue" else -amount
            self.pending_settlements[p["external_reference"]] = {
                **p, "agent_id": e.agent_id, "lineage_id": e.lineage_id, "generation_id": e.generation_id,
            }
            return
        settled = p.get("settles_reference")
        if settled and settled in self.pending_settlements:
            pending = self.pending_settlements.pop(settled)
            pc = self.counters[pending["generation_id"]][pending["agent_id"]]
            pc.unrealized -= float(pending["amount"]) if pending["type"] == "revenue" else -float(pending["amount"])
        if c is None:
            return
        kind, category = p["type"], p.get("category")
        signed = 0.0
        if kind == "revenue":
            c.gross_revenue += amount
            c.conversions += 1
            signed = amount
        elif kind == "refund":
            c.refunds += amount
            signed = -amount
        elif kind == "fee":
            c.fees += amount
            signed = -amount
        elif kind == "expense":
            if category == "external_spend":
                c.external_spend += amount
                self.spend_by_tick[tick] += amount
            elif category == "api_cloud":
                c.api_cloud_spend += amount
            elif category == "compute_imputed":
                c.compute_cost += amount
            else:
                c.other_costs += amount
            signed = -amount
        step_id = p.get("step_id")
        if step_id:
            # Attribute to the step's own generation bucket when known.
            gen = p.get("step_generation", e.generation_id)
            bucket = self.counters[gen][e.agent_id].step_net
            bucket[step_id] = bucket.get(step_id, 0.0) + signed

    def _on_milestone_validated(self, e: Event) -> None:
        c = self._c(e)
        if c:
            c.milestone_value += float(e.payload["value"])

    def _on_health_event(self, e: Event) -> None:
        self.health_events += 1

    def _on_emergency_stop(self, e: Event) -> None:
        self.halted = True
        self.halt_reason = e.payload.get("reason")

    def _on_farm_resumed(self, e: Event) -> None:
        self.halted = False
        self.halt_reason = None
