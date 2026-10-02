"""Capability gateway: the only path from an agent's intent to a side effect.

Every request is authenticated by token, classified, evaluated by the policy
engine, and logged (I8) before any adapter runs. A denied request cannot be
retried through another interface because no other interface exists.
"""

from __future__ import annotations

import math

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from runtime.tools.base import ToolContext, ToolError, ToolRegistry, ToolResult
from storage.events import EventStore, EventType, FarmState, digest

from ..accounting.resources import remaining_spend
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
    ) -> None:
        self.store = store
        self.state = state
        self.registry = registry
        self.authority = authority
        self.policy = policy
        self.new_id = new_id
        self.farm_spend_day = farm_spend_day
        self.on_hard_violation = on_hard_violation
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
        validation_errors = []
        try:
            action_class = adapter.classify(safe_args) if adapter else engine.classify_tool_name(str(tool))
        except Exception:
            return ToolResult(False, "denied", error="tool classification failed")
        if adapter:
            try:
                errors = adapter.validate(args)
            except Exception:
                errors = ["argument validation failed"]
            validation_errors = errors
            if errors and not engine.is_forbidden(action_class):
                self._decision(step, request_id, str(tool), "invalid", Decision.DENY,
                               "invalid tool arguments", 0.0, args)
                return ToolResult(False, "denied", error="invalid tool arguments")
        if approval_id is not None:
            approval = self.state.approvals.get(approval_id)
            requested = approval.get("request", {}) if approval else {}
            if (not approval or approval["status"] != "granted"
                    or approval["agent_id"] != step.agent_id or approval["generation"] != step.generation_id
                    or requested.get("tool") != tool or requested.get("args") != safe_args
                    or requested.get("step_id") != step.step_id
                    or requested.get("capability_epoch") != self.state.capability_epoch
                    or requested.get("policy_digest") != digest(engine.config)
                    or requested.get("adapter_digest") != (digest(adapter.spec) if getattr(adapter, "real_world", False) else None)):
                self._decision(step, request_id, str(tool), "invalid", Decision.DENY,
                               "invalid, stale, or consumed approval", 0.0, args)
                return ToolResult(False, "denied", error="invalid, stale, or consumed approval")
        try:
            spend = adapter.spend(safe_args) if adapter and not validation_errors else 0.0
        except Exception:
            return ToolResult(False, "denied", error="spend check failed")
        if not isinstance(spend, (int, float)) or isinstance(spend, bool) or not math.isfinite(spend) or spend < 0:
            self._decision(step, request_id, str(tool), action_class, Decision.DENY, "invalid spend", 0.0, args)
            return ToolResult(False, "denied", error="invalid spend")
        agent = self.state.agents[step.agent_id]
        counters = self.state.counter(step.generation_id, step.agent_id)
        req = CapabilityRequest(
            request_id=request_id, agent_id=step.agent_id, generation_id=step.generation_id, tool=str(tool),
            action_class=action_class, args=safe_args, spend=spend, granted=claims.capabilities,
            supports_spend_limit=bool(adapter and adapter.supports_spend_limit), approved=approval_id is not None,
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
                             "step_id": step.step_id, "capability_epoch": self.state.capability_epoch,
                             "policy_digest": digest(engine.config),
                             "adapter_digest": digest(adapter.spec) if getattr(adapter, "real_world", False) else None}},
                agent_id=step.agent_id, lineage_id=step.lineage_id, generation_id=step.generation_id,
                idempotency_key=f"approval:{approval}",
            )
            return ToolResult(False, "pending_approval", output={"approval_id": approval}, error=result.reason)
        if not result.permits_execution:
            return ToolResult(False, "denied", error=result.reason)
        if adapter is None:
            return ToolResult(False, "denied", error="adapter unavailable")
        return self._execute(adapter, str(tool), safe_args, step, request_id, result, approval_id)

    def dispatch_outbound(self, boundary) -> None:
        """Dispatch committed gateway intents only; not an agent capability."""
        from ..safety.boundary import SafetyError
        if self.store._depth:
            raise SafetyError("outbound dispatch cannot run inside a ledger transaction")
        events = self.store.iter_events()
        invoked = {e.payload["invocation_id"]: e for e in events if e.type is EventType.TOOL_INVOKED}
        attempted = {e.payload["invocation_id"] for e in events if e.type is EventType.OUTBOUND_ATTEMPTED}
        for queued in events:
            if queued.type is not EventType.OUTBOUND_QUEUED:
                continue
            p = queued.payload
            iid = p["invocation_id"]
            if iid in attempted:
                continue  # crash/timeout after send is ambiguous: never retry
            tool = self.registry.get(p["tool"])
            receipt = invoked.get(iid)
            agent = self.state.agents.get(queued.agent_id)
            eligible = (
                tool is not None and getattr(tool, "real_world", False)
                and queued.author == tool.author
                and receipt is not None and receipt.author == "supervisor" and receipt.payload["ok"]
                and receipt.payload["decision"] in ("allow", "allow_with_limit")
                and receipt.agent_id == queued.agent_id and receipt.payload["tool"] == p["tool"]
                and receipt.payload["input_digest"] == digest(p["args"])
                and receipt.payload.get("capability_epoch") == self.state.capability_epoch
                and receipt.payload.get("policy_digest") == digest(self.policy().config)
                and p["manifest_digest"] == digest(boundary.manifest)
                and not self.state.halted and agent is not None and agent.status == "running"
                and queued.generation_id == self.state.current_generation
                and p["tool"] in self.policy().capabilities
                and not self.policy().is_forbidden(tool.classify(p["args"]))
            )
            boundary.check()  # failure aborts the farm, not just this dispatch
            # Durable before transport; never emit this marker inside a transaction.
            marker = self.store.append(
                EventType.OUTBOUND_ATTEMPTED,
                {"invocation_id": iid, "tool": p["tool"], "cancelled": not eligible},
                agent_id=queued.agent_id, lineage_id=queued.lineage_id, generation_id=queued.generation_id,
                idempotency_key="attempted:" + iid,
            )
            if marker.existing:
                continue
            if eligible:
                try:
                    ok = tool._send(p["args"], iid)
                    status = "accepted" if ok else "provider_rejected_or_unknown"
                except Exception:
                    status = "unknown_requires_operator_reconciliation"
            else:
                status = "cancelled_requires_operator_reconciliation"
            self.store.append(
                EventType.OUTBOUND_RESULT,
                {"invocation_id": iid, "tool": p["tool"], "status": status},
                agent_id=queued.agent_id, lineage_id=queued.lineage_id, generation_id=queued.generation_id,
                idempotency_key="outbound_result:" + iid,
            )

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
            except Exception as exc:
                from ..safety.boundary import SafetyError
                if isinstance(exc, SafetyError):
                    raise  # a failed boundary check halts execution, including the farm
                if isinstance(exc, ToolError):
                    out = ToolResult(False, "error", error=str(exc))
                else:
                    out = ToolResult(False, "error", error="tool failure")
                    self.store.append(
                        EventType.HEALTH_EVENT,
                        {"component": f"tool:{tool}", "kind": "tool_exception"},
                        agent_id=step.agent_id, lineage_id=step.lineage_id, generation_id=step.generation_id,
                    )
        self.store.append(
            EventType.TOOL_INVOKED,
            {"invocation_id": invocation_id, "request_id": request_id, "tool": tool, "ok": out.ok,
             "status": out.status, "error": out.error, "input_digest": digest(args),
             "output_digest": digest(out.output), "step_id": step.step_id, "approval_id": approval_id,
             "decision": result.decision.value, "capability_epoch": self.state.capability_epoch,
             "policy_digest": digest(self.policy().config)},
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
