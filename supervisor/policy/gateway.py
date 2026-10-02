"""Capability gateway: the only path from an agent's intent to a side effect.

Every request is authenticated by a policy-bound token, classified, evaluated
by the policy engine, and logged before any adapter runs. Real-world adapters
are ordinary typed tools behind this same gateway; there is no second path.
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
from ..safety import control_plane_paths
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
        policy_fingerprint: Callable[[], str],
        new_id: Callable[[str], str],
        farm_spend_day: Callable[[], float],
        on_hard_violation: Callable[[str, int, str], None],
    ) -> None:
        self.store = store
        self.state = state
        self.registry = registry
        self.authority = authority
        self.policy = policy
        self.policy_fingerprint = policy_fingerprint
        self.new_id = new_id
        self.farm_spend_day = farm_spend_day
        self.on_hard_violation = on_hard_violation
        self._calls: dict[tuple[str, str, int], int] = defaultdict(int)
        self._calls_tick: int | None = None

    def invoke(self, token: str, tool: str, args: Any, step: StepContext, *, approval_id: str | None = None,
               external_action_id: str | None = None) -> ToolResult:
        request_id = self.new_id("request")
        if step.tick != self._calls_tick:
            self._calls.clear()
            self._calls_tick = step.tick

        fp = self.policy_fingerprint()
        try:
            claims = self.authority.verify(
                token,
                current_epoch=self.state.capability_epoch,
                generation_id=step.generation_id,
                policy_fingerprint=fp,
            )
        except InvalidToken as exc:
            self._decision(step, request_id, str(tool), "unknown", Decision.DENY,
                           f"invalid capability: {exc}", 0.0, args, policy_fingerprint=fp)
            return ToolResult(False, "denied", error="capability invalid or revoked")
        if claims.agent_id != step.agent_id:
            self._decision(step, request_id, str(tool), "security.token_misuse", Decision.DENY,
                           "token/agent mismatch", 0.0, args, policy_fingerprint=fp)
            return ToolResult(False, "denied", error="capability invalid")

        engine = self.policy()
        adapter = self.registry.get(tool) if isinstance(tool, str) else None
        safe_args = args if isinstance(args, dict) else {}

        base_action_class = adapter.classify(safe_args) if adapter else engine.classify_tool_name(str(tool))
        forbidden_fields = control_plane_paths(safe_args)
        # Preserve a more specific hard violation (for example host.shell) so
        # the ledger still records what the agent actually attempted. The
        # generic control-plane-argument class is for otherwise ordinary tools.
        if forbidden_fields and not engine.is_forbidden(base_action_class):
            action_class = "security.control_plane_argument"
            spend = 0.0
        else:
            action_class = base_action_class
            spend = adapter.spend(safe_args) if adapter else 0.0

        request = {
            "tool": str(tool),
            "args": safe_args,
            "action_class": action_class,
            "spend": spend,
            "step_id": step.step_id,
            "policy_fingerprint": fp,
        }
        request_digest = digest(request)
        approved = False
        if approval_id is not None:
            approval = self.state.approvals.get(approval_id)
            approved = bool(
                approval
                and (
                    approval.get("status") == "granted"
                    or (
                        external_action_id is not None
                        and approval.get("status") == "executed"
                        and (self.state.external_actions.get(external_action_id) or {}).get("approval_id") == approval_id
                    )
                )
                and approval.get("agent_id") == step.agent_id
                and approval.get("generation") == step.generation_id
                and approval.get("request_digest") == request_digest
                and approval.get("policy_fingerprint") == fp
            )
            if not approved:
                self._decision(
                    step, request_id, str(tool), action_class, Decision.DENY,
                    "approval does not match this exact request and safety policy", spend, args,
                    approval_id=approval_id, policy_fingerprint=fp,
                )
                return ToolResult(False, "denied",
                                  error="approval invalid or stale: it does not match this exact request and policy")

        agent = self.state.agents[step.agent_id]
        counters = self.state.counter(step.generation_id, step.agent_id)
        req = CapabilityRequest(
            request_id=request_id,
            agent_id=step.agent_id,
            generation_id=step.generation_id,
            tool=str(tool),
            action_class=action_class,
            args=safe_args,
            spend=spend,
            granted=claims.capabilities,
            supports_spend_limit=bool(adapter and adapter.supports_spend_limit),
            approved=approved,
        )
        ctx = PolicyContext(
            calls_this_tick=self._calls[(step.agent_id, str(tool), step.tick)],
            agent_spend_generation=counters.external_spend,
            agent_budget_remaining=remaining_spend(agent.budgets, counters),
            farm_spend_day=self.farm_spend_day(),
            agent_reserved_spend=self.state.reserved_spend(
                agent_id=step.agent_id, generation=step.generation_id, exclude_approval_id=approval_id,
                exclude_action_id=external_action_id,
            ),
            farm_reserved_spend=self.state.reserved_spend(
                exclude_approval_id=approval_id, exclude_action_id=external_action_id
            ),
        )
        result = engine.evaluate(req, ctx)
        if forbidden_fields and result.hard_violation:
            result = PolicyResult(
                result.decision,
                "agent attempted to supply supervisor-owned control-plane field(s): " + ", ".join(forbidden_fields),
                result.action_class,
                hard_violation=True,
                limit=result.limit,
            )
        self._decision(
            step, request_id, str(tool), action_class, result.decision, result.reason, spend, args,
            hard=result.hard_violation, approval_id=approval_id, policy_fingerprint=fp,
        )

        if result.hard_violation:
            self.store.append(
                EventType.POLICY_VIOLATION,
                {
                    "request_id": request_id,
                    "tool": str(tool),
                    "action_class": action_class,
                    "reason": result.reason,
                    "source": "gateway",
                    "step_id": step.step_id,
                    "policy_fingerprint": fp,
                },
                agent_id=step.agent_id,
                lineage_id=step.lineage_id,
                generation_id=step.generation_id,
                idempotency_key=f"violation:{request_id}",
            )
            self.on_hard_violation(step.agent_id, step.generation_id, result.reason)
            return ToolResult(False, "denied", error=result.reason)

        if result.decision is Decision.REQUIRE_HUMAN_APPROVAL:
            approval = self.new_id("approval")
            self.store.append(
                EventType.HUMAN_APPROVAL_REQUESTED,
                {
                    "approval_id": approval,
                    "reason": result.reason,
                    "request": request,
                    "request_digest": request_digest,
                    "policy_fingerprint": fp,
                },
                agent_id=step.agent_id,
                lineage_id=step.lineage_id,
                generation_id=step.generation_id,
                idempotency_key=f"approval:{approval}",
            )
            return ToolResult(False, "pending_approval", output={"approval_id": approval}, error=result.reason)

        if not result.permits_execution:
            return ToolResult(False, "denied", error=result.reason)
        if adapter is None:
            return ToolResult(False, "denied", error="capability has no registered adapter")
        return self._execute(
            adapter, str(tool), safe_args, step, request_id, result, approval_id, token, fp,
            request_digest, external_action_id,
        )

    def _execute(
        self,
        adapter: Any,
        tool: str,
        args: dict[str, Any],
        step: StepContext,
        request_id: str,
        result: PolicyResult,
        approval_id: str | None,
        token: str,
        policy_fingerprint: str,
        request_digest: str,
        external_action_id: str | None,
    ) -> ToolResult:
        self._calls[(step.agent_id, tool, step.tick)] += 1
        errors = adapter.validate(args)
        invocation_id = self.new_id("invocation")
        if errors:
            out = ToolResult(False, "error", error="; ".join(errors))
        else:
            ctx = ToolContext(
                agent_id=step.agent_id,
                lineage_id=step.lineage_id,
                generation_id=step.generation_id,
                step_id=step.step_id,
                invocation_id=invocation_id,
                tick=step.tick,
                now=step.now,
                workspace=step.workspace,
                max_spend=result.limit.get("max_spend"),
                capability_token=token,
                policy_fingerprint=policy_fingerprint,
                capability_epoch=self.state.capability_epoch,
                approval_id=approval_id,
                request_digest=request_digest,
                external_action_id=external_action_id,
            )
            try:
                out = ToolResult(True, "ok", output=adapter.invoke(args, ctx))
            except ToolError as exc:
                out = ToolResult(False, "error", error=str(exc))
            except Exception as exc:
                out = ToolResult(False, "error", error="tool failure")
                self.store.append(
                    EventType.HEALTH_EVENT,
                    {"component": f"tool:{tool}", "kind": "tool_exception", "detail": repr(exc)[:500]},
                    agent_id=step.agent_id,
                    lineage_id=step.lineage_id,
                    generation_id=step.generation_id,
                )
        self.store.append(
            EventType.TOOL_INVOKED,
            {
                "invocation_id": invocation_id,
                "request_id": request_id,
                "tool": tool,
                "ok": out.ok,
                "status": out.status,
                "error": out.error,
                "input_digest": digest(args),
                "output_digest": digest(out.output),
                "step_id": step.step_id,
                "approval_id": approval_id,
                "decision": result.decision.value,
                "policy_fingerprint": policy_fingerprint,
            },
            agent_id=step.agent_id,
            lineage_id=step.lineage_id,
            generation_id=step.generation_id,
            idempotency_key=f"tool:{invocation_id}",
        )
        artifact = out.output.pop("_artifact", None) if out.output else None
        if artifact:
            self.store.append(
                EventType.ARTIFACT_RECORDED,
                {**artifact, "invocation_id": invocation_id},
                agent_id=step.agent_id,
                lineage_id=step.lineage_id,
                generation_id=step.generation_id,
            )
        return out

    def _decision(
        self,
        step: StepContext,
        request_id: str,
        tool: str,
        action_class: str,
        decision: Decision,
        reason: str,
        spend: float,
        args: Any,
        *,
        hard: bool = False,
        approval_id: str | None = None,
        policy_fingerprint: str = "",
    ) -> None:
        self.store.append(
            EventType.POLICY_DECISION,
            {
                "request_id": request_id,
                "tool": tool,
                "action_class": action_class,
                "decision": decision.value,
                "reason": reason,
                "hard_violation": hard,
                "spend": spend,
                "args_digest": digest(args),
                "step_id": step.step_id,
                "tick": step.tick,
                "approval_id": approval_id,
                "policy_fingerprint": policy_fingerprint,
            },
            agent_id=step.agent_id,
            lineage_id=step.lineage_id,
            generation_id=step.generation_id,
            idempotency_key=f"decision:{request_id}",
        )
