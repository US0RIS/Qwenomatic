"""Configured real-world tools.

An agent invocation only queues a durable action after passing ToolGateway.
The supervisor later re-authorizes that exact action through ToolGateway and
then this adapter dispatches it through the separate safety gateway. A
pre-dispatch ledger marker is committed before network I/O, so a crash cannot
silently turn an unknown payment outcome into a retry.
"""

from __future__ import annotations

from typing import Any

from storage.events import EventStore, EventType
from supervisor.accounting import Attribution, ExternalSpendAdapter
from supervisor.policy.external import ExternalGatewayClient

from .base import ToolAdapter, ToolContext, ToolError


class ConfiguredExternalTool(ToolAdapter):
    def __init__(
        self,
        *,
        adapter_id: str,
        spec: dict[str, Any],
        client: ExternalGatewayClient,
        store: EventStore,
        spend_recorder: ExternalSpendAdapter,
    ) -> None:
        self.external_adapter_id = adapter_id
        self.name = str(spec["tool"])
        self.description = str(spec.get("description") or f"Approved external action {adapter_id}")
        self.action_class = str(spec["action_class"])
        self.args_schema = dict(spec.get("args_schema") or {})
        self.optional_args = frozenset(spec.get("optional_args") or [])
        self.spec = spec
        self.client = client
        self.store = store
        self.spend_recorder = spend_recorder
        self.kind = str(spec.get("kind") or "http")
        self.payment = dict(spec.get("payment") or {})

    def spend(self, args: dict[str, Any]) -> float:
        if self.kind != "payment":
            return 0.0
        field = str(self.payment.get("amount_field") or "amount")
        try:
            return max(0.0, float(args.get(field) or 0.0))
        except (TypeError, ValueError):
            return 0.0

    def validate(self, args: dict[str, Any]) -> list[str]:
        errors = super().validate(args)
        if errors or self.kind != "payment":
            return errors
        amount = self.spend(args)
        if amount <= 0:
            errors.append("payment amount must be positive")
        hard_cap = float(self.payment["hard_cap_per_action"])
        if amount > hard_cap:
            errors.append("payment exceeds fixed per-action hard cap")
        return errors

    def invoke(self, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        spend = self.spend(args)
        if ctx.external_action_id is None:
            action_id = ctx.invocation_id
            self.store.append(
                EventType.EXTERNAL_ACTION_REQUESTED,
                {
                    "action_id": action_id,
                    "adapter_id": self.external_adapter_id,
                    "tool": self.name,
                    "args": args,
                    "spend": spend,
                    "approval_id": ctx.approval_id,
                    "request_digest": ctx.request_digest,
                    "policy_fingerprint": ctx.policy_fingerprint,
                    "step_id": ctx.step_id,
                    "tick": ctx.tick,
                },
                agent_id=ctx.agent_id,
                lineage_id=ctx.lineage_id,
                generation_id=ctx.generation_id,
                idempotency_key=f"external-request:{action_id}",
            )
            return {"state": "queued", "action_id": action_id}

        action_id = ctx.external_action_id
        self.store.append(
            EventType.EXTERNAL_ACTION_DISPATCHING,
            {"action_id": action_id, "attempt": 1, "adapter_id": self.external_adapter_id},
            agent_id=ctx.agent_id,
            lineage_id=ctx.lineage_id,
            generation_id=ctx.generation_id,
            idempotency_key=f"external-dispatch:{action_id}",
        )
        response = self.client.invoke(self.external_adapter_id, self.name, args, ctx)
        state = str(response.get("state") or "uncertain")
        if state not in ("confirmed", "rejected", "uncertain"):
            state = "uncertain"
        reference = str(response.get("reference") or action_id)[:200]
        self.store.append(
            EventType.EXTERNAL_ACTION_RESULT,
            {
                "action_id": action_id,
                "adapter_id": self.external_adapter_id,
                "status": state,
                "result_reference": reference,
                "error": str(response.get("error") or "")[:300],
            },
            agent_id=ctx.agent_id,
            lineage_id=ctx.lineage_id,
            generation_id=ctx.generation_id,
            idempotency_key=f"external-result:{action_id}",
        )

        if state == "confirmed":
            if spend > 0:
                attr = Attribution(
                    agent_id=ctx.agent_id,
                    lineage_id=ctx.lineage_id,
                    generation_id=ctx.generation_id,
                    step_id=ctx.step_id,
                    tick=ctx.tick,
                    now=ctx.now,
                )
                self.spend_recorder.charge(
                    attr, spend, reference, adapter_id=self.external_adapter_id
                )
            result = response.get("result")
            return {
                "state": "confirmed",
                "reference": reference,
                **({"result": result} if isinstance(result, dict) else {}),
            }
        if state == "rejected":
            raise ToolError(str(response.get("error") or "external request rejected"))
        raise ToolError("external outcome is uncertain; spend remains reserved until an operator reconciles it")
