"""Event vocabulary for the append-only ledger (DESIGN §13)."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class EventType(str, Enum):
    # Lifecycle (I1: supervisor only)
    AGENT_CREATED = "agent_created"
    AGENT_STATUS_CHANGED = "agent_status_changed"
    AGENT_RETIRED = "agent_retired"
    AGENT_STEP_COMPLETED = "agent_step_completed"
    AGENT_CLAIM = "agent_claim"  # agent-authored text; never economic evidence
    STRATEGY_SUGGESTION = "strategy_suggestion"

    # Generations / evolution
    GENERATION_STARTED = "generation_started"
    GENERATION_CLOSE_STEP = "generation_close_step"
    GENERATION_CLOSED = "generation_closed"
    FITNESS_EVALUATED = "fitness_evaluated"
    SELECTION_DECIDED = "selection_decided"
    MUTATION_APPLIED = "mutation_applied"
    ATTRIBUTION_REPORT = "attribution_report"

    # Inference
    INFERENCE_JOB_SUBMITTED = "inference_job_submitted"
    INFERENCE_JOB_COMPLETED = "inference_job_completed"
    INFERENCE_JOB_FAILED = "inference_job_failed"
    INFERENCE_JOB_CANCELLED = "inference_job_cancelled"
    THINKING_DECISION = "thinking_decision"  # supervisor: per-step thinking decision and its reasons
    THINKING_ANCHOR = "thinking_anchor"      # supervisor: fixed validation anchor after a deep step

    # Scheduler
    SCHEDULER_ALLOCATION = "scheduler_allocation"

    # Policy / capability
    CAPABILITY_ISSUED = "capability_issued"
    CAPABILITIES_REVOKED = "capabilities_revoked"
    POLICY_DECISION = "policy_decision"
    POLICY_VIOLATION = "policy_violation"
    TOOL_INVOKED = "tool_invoked"
    HUMAN_APPROVAL_REQUESTED = "human_approval_requested"
    HUMAN_APPROVAL_RESOLVED = "human_approval_resolved"
    HUMAN_INTERVENTION = "human_intervention"
    SECURITY_SCOPE_APPROVED = "security_scope_approved"
    SECURITY_SCOPE_REVOKED = "security_scope_revoked"
    NETWORK_ATTESTED = "network_attested"

    # Economy
    OPPORTUNITY = "opportunity"
    FINANCIAL_EVENT = "financial_event"
    FINANCIAL_REJECTED = "financial_rejected"
    MILESTONE_VALIDATED = "milestone_validated"

    # Operations
    ARTIFACT_RECORDED = "artifact_recorded"
    HEALTH_EVENT = "health_event"
    EMERGENCY_STOP = "emergency_stop"
    FARM_RESUMED = "farm_resumed"
    SUPERVISOR_STARTED = "supervisor_started"


AUTHOR_SUPERVISOR = "supervisor"


def adapter_author(name: str) -> str:
    return f"adapter:{name}"


def agent_author(agent_id: str) -> str:
    return f"agent:{agent_id}"


# Event types an agent-attributed author may produce. Everything else must be
# written by the supervisor or a trusted adapter (I3, I4, I6).
AGENT_AUTHORABLE = frozenset({EventType.AGENT_CLAIM, EventType.STRATEGY_SUGGESTION})

# Economic evidence must come from a trusted adapter (I3).
ADAPTER_ONLY = frozenset(
    {EventType.FINANCIAL_EVENT, EventType.OPPORTUNITY, EventType.MILESTONE_VALIDATED}
)


@dataclass(frozen=True)
class Event:
    seq: int
    event_id: str
    type: EventType
    author: str
    agent_id: str | None
    lineage_id: str | None
    generation_id: int | None
    occurred_at: str
    recorded_at: str
    idempotency_key: str | None
    payload: dict[str, Any]
    prev_hash: str
    hash: str
    # True when append() found an existing row for the idempotency key and
    # returned it instead of writing a new one. Not persisted.
    existing: bool = field(default=False, compare=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "event_id": self.event_id,
            "type": self.type.value,
            "author": self.author,
            "agent_id": self.agent_id,
            "lineage_id": self.lineage_id,
            "generation_id": self.generation_id,
            "occurred_at": self.occurred_at,
            "recorded_at": self.recorded_at,
            "idempotency_key": self.idempotency_key,
            "payload": self.payload,
            "prev_hash": self.prev_hash,
            "hash": self.hash,
        }
