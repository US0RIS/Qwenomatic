"""Supervisor-owned trusted adapters: the only sources of economic evidence.

In Phase 0 the payment processor and ad platform are backed by the simulated
market. Phase 2 replaces the backend with one narrow, approved real-world
adapter behind the same interface.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable

from storage.events import EventStore, EventType, FarmState

from .ledger import Ledger, TrustedAdapter


@dataclass
class Attribution:
    """Who an economic event belongs to. Built by the supervisor, not agents."""

    agent_id: str
    lineage_id: str
    generation_id: int
    step_id: str | None
    tick: int
    now: datetime


class PaymentProcessorAdapter(TrustedAdapter):
    """Records sales as unrealized forecasts, then settles them when due."""

    name = "payments"

    def __init__(self, ledger: Ledger, state: FarmState) -> None:
        self.ledger = ledger
        self.state = state

    def on_sale(self, attr: Attribution, outcome: Any) -> None:
        details = getattr(outcome, 'details', None) or {}
        ref = f"sale:{outcome.opportunity_id}"
        common = dict(
            agent_id=attr.agent_id, lineage_id=attr.lineage_id, generation_id=attr.generation_id,
            occurred_at=attr.now.isoformat(), observed_at=attr.now.isoformat(), step_id=attr.step_id,
            step_generation=attr.generation_id, tick=attr.tick,
        )
        if outcome.settle_at <= attr.now and not details.get('payment_failed'):
            self._settle(ref, outcome.price, outcome.fee, outcome.refund, outcome.refund_delay_hours, common, attr.now,
                         chargeback=details.get('chargeback', False), dispute_fee=details.get('chargeback_fee', 0.))
            return
        self.ledger.record(
            self, type="revenue", category="gross_revenue", amount=outcome.price, external_reference=ref,
            status="unrealized", confidence=0.9,
            extra={"due_at": outcome.settle_at.isoformat(), "fee": outcome.fee, "refund": outcome.refund,
                   "refund_delay_hours": outcome.refund_delay_hours, "kind": "sale",
                   'chargeback': details.get('chargeback', False), 'dispute_fee': details.get('chargeback_fee', 0.),
                   'payment_failed': details.get('payment_failed', False)},
            **common,
        )

    def _settle(self, ref: str, price: float, fee: float, refund: bool, refund_delay: float,
                common: dict[str, Any], now: datetime, settles: str | None = None,
                chargeback: bool = False, dispute_fee: float = 0.) -> None:
        self.ledger.record(
            self, type="revenue", category="gross_revenue", amount=price, external_reference=f"{ref}:settled",
            settles_reference=settles, **common,
        )
        if fee:
            self.ledger.record(
                self, type="fee", category="payment_fee", amount=fee, external_reference=f"{ref}:fee", **common
            )
        if refund:
            self.ledger.record(
                self, type="refund", category="chargeback" if chargeback else "refund", amount=price, external_reference=f"{ref}:refund",
                status="unrealized", confidence=0.9,
                extra={"due_at": (now + timedelta(hours=refund_delay)).isoformat(), "kind": "refund", 'dispute_fee': dispute_fee},
                **common,
            )

    def reconcile(self, now: datetime, generation_id: int, tick: int) -> int:
        """Settle every pending item that is due. Idempotent by reference."""
        settled = 0
        due = [
            p for p in list(self.state.pending_settlements.values())
            if p.get("source") == self.name and datetime.fromisoformat(p["due_at"]) <= now
        ]
        for p in sorted(due, key=lambda x: (x["due_at"], x["external_reference"])):
            common = dict(
                agent_id=p["agent_id"], lineage_id=p["lineage_id"], generation_id=generation_id,
                occurred_at=p["occurred_at"], observed_at=now.isoformat(), step_id=p.get("step_id"),
                step_generation=p.get("step_generation"), tick=tick,
            )
            ref = p["external_reference"]
            if p.get('payment_failed'):
                # Unwind an unpaid forecast without fabricating cash or a sale.
                self.ledger.record(self, type='expense', category='other_variable', amount=0,
                    external_reference=f'{ref}:default', settles_reference=ref,
                    extra={'kind': 'invoice_default'}, **common)
            elif p.get("kind") == "refund":
                self.ledger.record(
                    self, type="refund", category=p['category'], amount=p["amount"],
                    external_reference=f"{ref}:settled", settles_reference=ref, **common,
                )
                if p.get('dispute_fee', 0):
                    self.ledger.record(self, type='fee', category='platform_fee', amount=p['dispute_fee'],
                                       external_reference=f'{ref}:dispute-fee', **common)
            else:
                self._settle(ref, p["amount"], p.get("fee", 0.0), p.get("refund", False),
                             p.get("refund_delay_hours", 0.0), common, now, settles=ref,
                             chargeback=p.get('chargeback', False), dispute_fee=p.get('dispute_fee', 0.))
            settled += 1
        return settled


class AdSpendMeter(TrustedAdapter):
    """External acquisition spend charged by the (simulated) ad platform."""

    name = "ad_platform"

    def __init__(self, ledger: Ledger) -> None:
        self.ledger = ledger

    def charge(self, attr: Attribution, amount: float, reference: str) -> None:
        if amount <= 0:
            return
        self.ledger.record(
            self, agent_id=attr.agent_id, lineage_id=attr.lineage_id, generation_id=attr.generation_id,
            type="expense", category="external_spend", amount=amount, external_reference=f"ad:{reference}",
            occurred_at=attr.now.isoformat(), observed_at=attr.now.isoformat(), step_id=attr.step_id,
            step_generation=attr.generation_id, tick=attr.tick,
        )


class MarketCostMeter(TrustedAdapter):
    """Acquired inputs, imputed fulfillment labor and time-based overhead."""
    name = 'market_costs'

    def __init__(self, ledger):
        self.ledger = ledger

    def charge(self, attr, reference, costs, *, step_generation=None):
        for kind, amount in costs.items():
            if amount <= 0:
                continue
            self.ledger.record(
                self, agent_id=attr.agent_id, lineage_id=attr.lineage_id, generation_id=attr.generation_id,
                type='expense', category='external_spend' if kind == 'fulfillment' else 'other_variable',
                amount=amount, external_reference=f'market-cost:{reference}:{kind}',
                occurred_at=attr.now.isoformat(), observed_at=attr.now.isoformat(),
                step_id=attr.step_id, step_generation=attr.generation_id if step_generation is None else step_generation, tick=attr.tick,
                extra={'kind': kind, 'simulation': True})


class ComputeMeter(TrustedAdapter):
    """Imputed local compute cost and metered cloud cost per inference job."""

    name = "compute_meter"

    def __init__(self, ledger: Ledger, usd_per_gpu_hour: Callable[[], float]) -> None:
        self.ledger = ledger
        self.usd_per_gpu_hour = usd_per_gpu_hour

    def charge(self, attr: Attribution, job_id: str, gpu_seconds: float, cloud_usd: float = 0.0) -> None:
        common = dict(
            agent_id=attr.agent_id, lineage_id=attr.lineage_id, generation_id=attr.generation_id,
            occurred_at=attr.now.isoformat(), observed_at=attr.now.isoformat(), step_id=attr.step_id,
            step_generation=attr.generation_id, tick=attr.tick,
        )
        cost = gpu_seconds / 3600 * self.usd_per_gpu_hour()
        if cost > 0:
            self.ledger.record(self, type="expense", category="compute_imputed", amount=cost,
                               external_reference=f"gpu:{job_id}", **common)
        if cloud_usd > 0:
            self.ledger.record(self, type="expense", category="api_cloud", amount=cloud_usd,
                               external_reference=f"cloud:{job_id}", **common)


class HumanLaborMeter(TrustedAdapter):
    """Imputes the cost of operator time spent on an agent's behalf."""

    name = "human_labor"

    def __init__(self, ledger: Ledger, usd_per_intervention: Callable[[], float]) -> None:
        self.ledger = ledger
        self.usd_per_intervention = usd_per_intervention

    def charge(self, attr: Attribution, reference: str) -> None:
        cost = self.usd_per_intervention()
        if cost > 0:
            self.ledger.record(
                self, agent_id=attr.agent_id, lineage_id=attr.lineage_id, generation_id=attr.generation_id,
                type="expense", category="other_variable", amount=cost, external_reference=f"labor:{reference}",
                occurred_at=attr.now.isoformat(), observed_at=attr.now.isoformat(), tick=attr.tick,
                extra={"kind": "human_labor"},
            )


class MarketObserver(TrustedAdapter):
    """Records opportunities (offers made) as observed by the market."""

    name = "market"

    def __init__(self, store: EventStore) -> None:
        self.store = store

    def opportunity(self, attr: Attribution, opportunity_id: str, details: dict[str, Any]) -> None:
        self.store.append(
            EventType.OPPORTUNITY,
            {"opportunity_id": opportunity_id, "step_id": attr.step_id, "tick": attr.tick, **details},
            author=self.author, agent_id=attr.agent_id, lineage_id=attr.lineage_id,
            generation_id=attr.generation_id, occurred_at=attr.now.isoformat(),
            idempotency_key=f"opp:{opportunity_id}",
        )


class MilestoneValidator(TrustedAdapter):
    """Independently validated progress for delayed-feedback strategies."""

    name = "milestones"

    def __init__(self, store: EventStore) -> None:
        self.store = store

    def validate(self, attr: Attribution, milestone: str, value: float, evidence_reference: str) -> None:
        self.store.append(
            EventType.MILESTONE_VALIDATED,
            {"milestone": milestone, "value": float(value), "evidence_reference": evidence_reference},
            author=self.author, agent_id=attr.agent_id, lineage_id=attr.lineage_id,
            generation_id=attr.generation_id, occurred_at=attr.now.isoformat(),
            idempotency_key=f"milestone:{evidence_reference}",
        )


class ComplianceMonitor(TrustedAdapter):
    """Post-hoc violation reports (e.g. platform abuse reports).

    Some violations are only observable after the fact. A confirmed report is
    a hard policy failure exactly like a gateway denial.
    """

    name = "compliance"

    def __init__(self, store: EventStore) -> None:
        self.store = store

    def report(self, attr: Attribution, action_class: str, reason: str, evidence_reference: str) -> None:
        self.store.append(
            EventType.POLICY_VIOLATION,
            {"action_class": action_class, "reason": reason, "source": "compliance_monitor",
             "evidence_reference": evidence_reference, "tool": None},
            author=self.author, agent_id=attr.agent_id, lineage_id=attr.lineage_id,
            generation_id=attr.generation_id, occurred_at=attr.now.isoformat(),
            idempotency_key=f"compliance:{evidence_reference}",
        )
