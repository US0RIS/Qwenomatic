"""Capability gateway: the only path from an agent's intent to a side effect.

Every request is authenticated by token, classified, evaluated by the policy
engine, and logged (I8) before any adapter runs. A denied request cannot be
retried through another interface because no other interface exists.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from runtime.tools.base import ToolContext, ToolError, ToolRegistry, ToolResult
from storage.events import EventStore, EventType, FarmState, digest

from ..accounting.resources import remaining_spend
from ..security import SecurityScopeError
from .capabilities import InvalidToken, TokenAuthority
from .engine import CapabilityRequest, Decision, PolicyContext, PolicyEngine, PolicyResult


@dataclass(frozen=True)
class StepContext:
    agent_id: str
    lineage_id: str
    generation_id: int
    step_id: str | None
    tick: int
    now: datetime
    workspace: Path


class ToolGateway:
    def __init__(
        self,
        *,
        store: EventStore,
        state: FarmState,
        registry: ToolRegistry,
        authority: TokenAuthority,
        policy: Callable[[], PolicyEngine],
        new_id: Callable[[str], str],
        farm_spend_day: Callable[[], float],
        on_hard_violation: Callable[[str, int, str], None],
        security=None,
    ) -> None:
        self.store = store
        self.state = state
        self.registry = registry
        self.authority = authority
        self.policy = policy
        self.new_id = new_id
        self.farm_spend_day = farm_spend_day
        self.on_hard_violation = on_hard_violation
        self.security = security
        self._calls: dict[tuple[str, str, int], int] = defaultdict(int)
        self._calls_tick: int | None = None

    # --------------------------------------------------------------- invoke
    def invoke(self, token: str, tool: str, args: Any, step: StepContext, *, approval_id: str | None = None) -> ToolResult:
        request_id = self.new_id("request")
        if step.tick != self._calls_tick:  # rate limits are per tick; drop older counts
            self._calls.clear()
            self._calls_tick = step.tick
        try:
            claims = self.authority.verify(
                token, current_epoch=self.state.capability_epoch, generation_id=step.generation_id
            )
        except InvalidToken as exc:
            self._decision(step, request_id, tool, "unknown", Decision.DENY, f"invalid capability: {exc}", 0.0, args)
            return ToolResult(False, "denied", error="capability invalid or revoked")
        if claims.agent_id != step.agent_id:
            self._decision(step, request_id, tool, "security.token_misuse", Decision.DENY, "token/agent mismatch", 0.0, args)
            return ToolResult(False, "denied", error="capability invalid")

        engine = self.policy()
        adapter = self.registry.get(tool) if isinstance(tool, str) else None
        safe_args = args if isinstance(args, dict) else {}
        if adapter is not None and bool(getattr(adapter, "real_world", False)):
            try:
                if self.security is None:
                    raise SecurityScopeError("real-world adapter has no supervisor security registry")
                self.security.validate_adapter(adapter, require_approval=True)
            except SecurityScopeError as exc:
                # An unapproved scope is an operator/configuration boundary, not
                # evidence that the agent itself committed a hard violation.
                self._decision(
                    step, request_id, str(tool), "external.scope_unapproved",
                    Decision.DENY, str(exc), 0.0, safe_args,
                )
                return ToolResult(False, "denied", error="real-world scope is not operator-approved")
        action_class = adapter.classify(safe_args) if adapter else engine.classify_tool_name(str(tool))
        spend = adapter.spend(safe_args) if adapter else 0.0
        if adapter is not None and bool(getattr(adapter, "outbound_payment", False)):
            # Security accounting for a transfer is canonical: a payment adapter
            # cannot under-report its own amount through spend(). Invalid/missing
            # amounts still fail the adapter schema before any side effect.
            raw_amount = safe_args.get("amount")
            if isinstance(raw_amount, (int, float)) and not isinstance(raw_amount, bool):
                spend = max(float(spend), max(0.0, float(raw_amount)))
        if adapter is not None and self.security is not None and self.security.action_exceeds_hard_cap(adapter, spend):
            # A fixed payment cap is a hard boundary, not a spend-budget suggestion.
            action_class = "finance.transfer_unapproved"
        force_approval = bool(
            adapter is not None
            and self.security is not None
            and self.security.action_requires_approval(adapter, spend)
        )
        agent = self.state.agents[step.agent_id]
        counters = self.state.counter(step.generation_id, step.agent_id)
        req = CapabilityRequest(
            request_id=request_id, agent_id=step.agent_id, generation_id=step.generation_id, tool=str(tool),
            action_class=action_class, args=safe_args, spend=spend, granted=claims.capabilities,
            supports_spend_limit=bool(adapter and adapter.supports_spend_limit), approved=approval_id is not None,
            force_approval=force_approval,
        )
        ctx = PolicyContext(
            calls_this_tick=self._calls[(step.agent_id, str(tool), step.tick)],
            agent_spend_generation=counters.external_spend,
            agent_budget_remaining=remaining_spend(agent.budgets, counters),
            farm_spend_day=self.farm_spend_day(),
        )
        result = engine.evaluate(req, ctx)
        self._decision(step, request_id, str(tool), action_class, result.decision, result.reason, spend, args,
                       hard=result.hard_violation, approval_id=approval_id)

        if result.hard_violation:
            self.store.append(
                EventType.POLICY_VIOLATION,
                {"request_id": request_id, "tool": str(tool), "action_class": action_class,
                 "reason": result.reason, "source": "gateway", "step_id": step.step_id},
                agent_id=step.agent_id, lineage_id=step.lineage_id, generation_id=step.generation_id,
                idempotency_key=f"violation:{request_id}",
            )
            self.on_hard_violation(step.agent_id, step.generation_id, result.reason)
            return ToolResult(False, "denied", error=result.reason)
        if result.decision is Decision.REQUIRE_HUMAN_APPROVAL:
            approval = self.new_id("approval")
            self.store.append(
                EventType.HUMAN_APPROVAL_REQUESTED,
                {"approval_id": approval, "reason": result.reason,
                 "request": {"tool": str(tool), "args": safe_args, "action_class": action_class, "spend": spend,
                             "step_id": step.step_id}},
                agent_id=step.agent_id, lineage_id=step.lineage_id, generation_id=step.generation_id,
                idempotency_key=f"approval:{approval}",
            )
            return ToolResult(False, "pending_approval", output={"approval_id": approval}, error=result.reason)
        if not result.permits_execution:
            return ToolResult(False, "denied", error=result.reason)
        return self._execute(adapter, str(tool), safe_args, step, request_id, result, approval_id)

    # ------------------------------------------------------------ internals
    def _execute(self, adapter, tool: str, args: dict[str, Any], step: StepContext, request_id: str,
                 result: PolicyResult, approval_id: str | None) -> ToolResult:
        self._calls[(step.agent_id, tool, step.tick)] += 1
        errors = adapter.validate(args)
        invocation_id = self.new_id("invocation")
        if errors:
            out = ToolResult(False, "error", error="; ".join(errors))
        else:
            ctx = ToolContext(
                agent_id=step.agent_id, lineage_id=step.lineage_id, generation_id=step.generation_id,
                step_id=step.step_id, invocation_id=invocation_id, tick=step.tick, now=step.now,
                workspace=step.workspace, max_spend=result.limit.get("max_spend"),
            )
            try:
                out = ToolResult(True, "ok", output=adapter.invoke(args, ctx))
            except ToolError as exc:
                out = ToolResult(False, "error", error=str(exc))
            except Exception as exc:  # adapter bug or external failure: never crash the farm
                out = ToolResult(False, "error", error="tool failure")
                self.store.append(
                    EventType.HEALTH_EVENT,
                    {"component": f"tool:{tool}", "kind": "tool_exception", "detail": repr(exc)[:500]},
                    agent_id=step.agent_id, lineage_id=step.lineage_id, generation_id=step.generation_id,
                )
        self.store.append(
            EventType.TOOL_INVOKED,
            {"invocation_id": invocation_id, "request_id": request_id, "tool": tool, "ok": out.ok,
             "status": out.status, "error": out.error, "input_digest": digest(args),
             "output_digest": digest(out.output), "step_id": step.step_id, "approval_id": approval_id,
             "decision": result.decision.value},
            agent_id=step.agent_id, lineage_id=step.lineage_id, generation_id=step.generation_id,
            idempotency_key=f"tool:{invocation_id}",
        )
        artifact = out.output.pop("_artifact", None) if out.output else None
        if artifact:
            self.store.append(
                EventType.ARTIFACT_RECORDED, {**artifact, "invocation_id": invocation_id},
                agent_id=step.agent_id, lineage_id=step.lineage_id, generation_id=step.generation_id,
            )
        return out

    def _decision(self, step: StepContext, request_id: str, tool: str, action_class: str, decision: Decision,
                  reason: str, spend: float, args: Any, *, hard: bool = False, approval_id: str | None = None) -> None:
        self.store.append(
            EventType.POLICY_DECISION,
            {"request_id": request_id, "tool": tool, "action_class": action_class, "decision": decision.value,
             "reason": reason, "hard_violation": hard, "spend": spend, "args_digest": digest(args),
             "step_id": step.step_id, "tick": step.tick, "approval_id": approval_id},
            agent_id=step.agent_id, lineage_id=step.lineage_id, generation_id=step.generation_id,
            idempotency_key=f"decision:{request_id}",
        )
