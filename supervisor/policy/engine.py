"""Policy engine (DESIGN §11).

agent -> capability request -> policy engine -> tool adapter -> external system

Hard constraints are eligibility requirements: a request in a forbidden
action class is denied and recorded as a hard violation, which disqualifies
the agent regardless of revenue (I5).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from fnmatch import fnmatchcase
from typing import Any


class Decision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_HUMAN_APPROVAL = "require_human_approval"
    ALLOW_WITH_LIMIT = "allow_with_limit"


@dataclass(frozen=True)
class CapabilityRequest:
    request_id: str
    agent_id: str
    generation_id: int
    tool: str
    action_class: str
    args: dict[str, Any]
    spend: float
    granted: frozenset[str]
    supports_spend_limit: bool = False
    approved: bool = False


@dataclass(frozen=True)
class PolicyContext:
    calls_this_tick: int = 0
    agent_spend_generation: float = 0.0
    agent_budget_remaining: float = float("inf")
    farm_spend_day: float = 0.0
    agent_reserved_spend: float = 0.0
    farm_reserved_spend: float = 0.0


@dataclass(frozen=True)
class PolicyResult:
    decision: Decision
    reason: str
    action_class: str
    hard_violation: bool = False
    limit: dict[str, Any] = field(default_factory=dict)

    @property
    def permits_execution(self) -> bool:
        return self.decision in (Decision.ALLOW, Decision.ALLOW_WITH_LIMIT)


class PolicyEngine:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.version = config.get("version", 1)
        self.capabilities: dict[str, dict[str, Any]] = config.get("capabilities", {})
        self.forbidden: list[str] = list(config.get("forbidden_action_classes", []))
        self.tool_classes: dict[str, str] = dict(config.get("tool_action_classes", {}))
        self.approval_classes: list[str] = list(config.get("approval_required_action_classes", []))
        self.unknown_is_violation = bool(config.get("unknown_tool_is_violation", False))
        spending = config.get("spending", {})
        self.material_threshold = float(spending.get("material_spend_threshold", float("inf")))
        self.per_agent_limit = float(spending.get("per_agent_generation_limit", float("inf")))
        self.farm_daily_limit = float(spending.get("farm_daily_limit", float("inf")))

    def classify_tool_name(self, tool: str) -> str:
        for pattern, action_class in self.tool_classes.items():
            if fnmatchcase(tool, pattern):
                return action_class
        return "unknown"

    def is_forbidden(self, action_class: str) -> bool:
        return any(fnmatchcase(action_class, p) for p in self.forbidden)

    def needs_approval(self, action_class: str) -> bool:
        return any(fnmatchcase(action_class, p) for p in self.approval_classes)

    def granted_capabilities(self) -> list[str]:
        return sorted(self.capabilities)

    def evaluate(self, req: CapabilityRequest, ctx: PolicyContext) -> PolicyResult:
        ac = req.action_class
        if self.is_forbidden(ac):
            return PolicyResult(Decision.DENY, f"forbidden action class {ac}", ac, hard_violation=True)
        if req.tool not in req.granted or req.tool not in self.capabilities:
            hard = ac == "unknown" and self.unknown_is_violation
            return PolicyResult(Decision.DENY, "capability not granted", ac, hard_violation=hard)

        cap = self.capabilities[req.tool] or {}
        rate = cap.get("rate_limit_per_tick")
        if rate is not None and ctx.calls_this_tick >= int(rate):
            return PolicyResult(Decision.DENY, "rate limit", ac)

        if req.spend > 0:
            hard_cap = cap.get("max_spend_per_call")
            if hard_cap is not None and req.spend > float(hard_cap):
                return PolicyResult(Decision.DENY, "per-action spend hard cap", ac)

        if not req.approved:
            if self.needs_approval(ac):
                return PolicyResult(Decision.REQUIRE_HUMAN_APPROVAL, f"{ac} requires operator approval", ac)
            threshold = float(cap.get("material_spend_threshold", self.material_threshold))
            if req.spend > threshold:
                return PolicyResult(Decision.REQUIRE_HUMAN_APPROVAL, "material spend requires approval", ac)

        if req.spend > 0:
            farm_committed = ctx.farm_spend_day + ctx.farm_reserved_spend
            if farm_committed + req.spend > self.farm_daily_limit:
                return PolicyResult(Decision.DENY, "farm daily spend ceiling", ac)
            remaining = min(
                self.per_agent_limit - ctx.agent_spend_generation - ctx.agent_reserved_spend,
                ctx.agent_budget_remaining - ctx.agent_reserved_spend,
            )
            remaining = max(0.0, remaining)
            if req.spend > remaining:
                if req.supports_spend_limit and remaining > 0:
                    return PolicyResult(
                        Decision.ALLOW_WITH_LIMIT,
                        "spend limited to remaining budget",
                        ac,
                        limit={"max_spend": round(remaining, 6)},
                    )
                return PolicyResult(Decision.DENY, "agent spend limit", ac)
        return PolicyResult(Decision.ALLOW, "allowed", ac)
