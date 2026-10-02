"""Human approval gates (README "Security model")."""

from __future__ import annotations

from typing import Any

from storage.events import EventStore, EventType, FarmState


class ApprovalError(Exception):
    pass


def pending(state: FarmState) -> list[dict[str, Any]]:
    return [a for a in state.approvals.values() if a["status"] == "pending"]


def resolve(store: EventStore, state: FarmState, approval_id: str, *, granted: bool, operator: str,
            note: str = "", policy_fingerprint: str | None = None) -> None:
    approval = state.approvals.get(approval_id)
    if approval is None:
        raise ApprovalError(f"no approval {approval_id}")
    if approval["status"] != "pending":
        raise ApprovalError(f"approval {approval_id} already {approval['status']}")
    if policy_fingerprint is not None and approval.get("policy_fingerprint") != policy_fingerprint:
        raise ApprovalError("approval belongs to a different safety-policy fingerprint")
    agent = state.agents.get(approval["agent_id"])
    with store.transaction():
        store.append(
            EventType.HUMAN_APPROVAL_RESOLVED,
            {"approval_id": approval_id, "granted": granted, "operator": operator, "note": note},
            author=f"operator:{operator}", agent_id=approval["agent_id"],
            lineage_id=agent.lineage_id if agent else None, generation_id=approval["generation"],
            idempotency_key=f"approval_resolved:{approval_id}",
        )
        # Operator time is measured and attributed (README "Fitness").
        store.append(
            EventType.HUMAN_INTERVENTION,
            {"kind": "approval", "approval_id": approval_id, "granted": granted, "operator": operator},
            author=f"operator:{operator}", agent_id=approval["agent_id"],
            lineage_id=agent.lineage_id if agent else None, generation_id=approval["generation"],
        )
