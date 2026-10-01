"""Economic ledger (DESIGN §8).

Only trusted adapters registered by the supervisor can record financial
events (invariant I3). Agent-authored text is never a revenue source: the
event store itself refuses FINANCIAL_EVENT rows that are not adapter-authored.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Iterable

from storage.events import Event, EventStore, EventType, adapter_author

FIN_TYPES = ("revenue", "expense", "refund", "fee")
EXPENSE_CATEGORIES = ("external_spend", "api_cloud", "compute_imputed", "other_variable")
CATEGORY_BY_TYPE = {
    "revenue": ("gross_revenue",),
    "refund": ("refund", "chargeback"),
    "fee": ("payment_fee", "platform_fee"),
    "expense": EXPENSE_CATEGORIES,
}
STATUSES = ("realized", "unrealized")


class LedgerError(Exception):
    pass


class UntrustedSourceError(LedgerError):
    pass


class TrustedAdapter:
    """Base for supervisor-owned adapters that are sources of truth."""

    name: str = "adapter"

    @property
    def author(self) -> str:
        return adapter_author(self.name)


@dataclass
class Posting:
    account: str
    debit: float
    credit: float


class Ledger:
    def __init__(self, store: EventStore, *, farm_controlled_accounts: Iterable[str] = (), currency: str = "USD"):
        self.store = store
        self.currency = currency
        self.farm_controlled_accounts = set(farm_controlled_accounts)
        self._adapters: dict[str, TrustedAdapter] = {}

    # --------------------------------------------------------------- adapters
    def register_adapter(self, adapter: TrustedAdapter) -> None:
        if adapter.name in self._adapters and self._adapters[adapter.name] is not adapter:
            raise LedgerError(f"adapter {adapter.name!r} already registered")
        self._adapters[adapter.name] = adapter

    def is_trusted(self, adapter: Any) -> bool:
        return isinstance(adapter, TrustedAdapter) and self._adapters.get(adapter.name) is adapter

    # ----------------------------------------------------------------- record
    def record(
        self,
        adapter: TrustedAdapter,
        *,
        agent_id: str | None,
        lineage_id: str | None,
        generation_id: int | None,
        type: str,
        category: str,
        amount: float,
        external_reference: str,
        occurred_at: str,
        observed_at: str,
        confidence: float = 1.0,
        status: str = "realized",
        step_id: str | None = None,
        step_generation: int | None = None,
        settles_reference: str | None = None,
        counterparty: str | None = None,
        tick: int | None = None,
        extra: dict[str, Any] | None = None,
    ) -> Event | None:
        if not self.is_trusted(adapter):
            raise UntrustedSourceError(f"{getattr(adapter, 'name', adapter)!r} is not a registered trusted adapter")
        if type not in FIN_TYPES:
            raise LedgerError(f"unknown financial type {type!r}")
        if category not in CATEGORY_BY_TYPE[type]:
            raise LedgerError(f"category {category!r} invalid for {type!r}")
        if status not in STATUSES:
            raise LedgerError(f"unknown status {status!r}")
        if not isinstance(amount, (int, float)) or not math.isfinite(amount) or amount < 0:
            raise LedgerError(f"amount must be a finite non-negative number, got {amount!r}")
        if not 0.0 <= confidence <= 1.0:
            raise LedgerError("confidence must be within [0, 1]")
        payload = {
            "type": type,
            "category": category,
            "amount": round(float(amount), 6),
            "currency": self.currency,
            "external_reference": external_reference,
            "occurred_at": occurred_at,
            "observed_at": observed_at,
            "confidence": confidence,
            "status": status,
            "source": adapter.name,
            "step_id": step_id,
            "step_generation": step_generation,
            "settles_reference": settles_reference,
            "counterparty": counterparty,
            "tick": tick,
            **(extra or {}),
        }
        if type == "revenue" and counterparty in self.farm_controlled_accounts:
            # Transfers between farm-controlled accounts are never revenue.
            self.store.append(
                EventType.FINANCIAL_REJECTED,
                {**payload, "reason": "internal_transfer"},
                author=adapter.author, agent_id=agent_id, lineage_id=lineage_id, generation_id=generation_id,
                idempotency_key=f"finrej:{adapter.name}:{external_reference}",
            )
            return None
        return self.store.append(
            EventType.FINANCIAL_EVENT,
            payload,
            author=adapter.author,
            agent_id=agent_id,
            lineage_id=lineage_id,
            generation_id=generation_id,
            occurred_at=occurred_at,
            idempotency_key=f"fin:{adapter.name}:{external_reference}",
        )

    # ------------------------------------------------------- double entry
    @staticmethod
    def postings(event: Event) -> list[Posting]:
        p = event.payload
        amt = float(p["amount"])
        if p["status"] == "unrealized":
            if p["type"] == "revenue":
                return [Posting("asset:receivable_pending", amt, 0), Posting("income:unrealized", 0, amt)]
            return [Posting("income:unrealized", amt, 0), Posting("asset:receivable_pending", 0, amt)]
        entries: list[Posting] = []
        if p["type"] == "revenue":
            entries += [Posting("asset:cash", amt, 0), Posting("income:gross_revenue", 0, amt)]
        elif p["type"] == "refund":
            entries += [Posting("contra:refunds", amt, 0), Posting("asset:cash", 0, amt)]
        elif p["type"] == "fee":
            entries += [Posting("expense:fees", amt, 0), Posting("asset:cash", 0, amt)]
        else:
            credit = "equity:imputed" if p["category"] == "compute_imputed" else "asset:cash"
            entries += [Posting(f"expense:{p['category']}", amt, 0), Posting(credit, 0, amt)]
        return entries

    @staticmethod
    def trial_balance(events: Iterable[Event]) -> dict[str, float]:
        balances: dict[str, float] = defaultdict(float)
        for e in events:
            if e.type is not EventType.FINANCIAL_EVENT:
                continue
            for posting in Ledger.postings(e):
                balances[posting.account] += posting.debit - posting.credit
        return dict(balances)

    @staticmethod
    def pnl(events: Iterable[Event]) -> dict[str, float]:
        """Headline P&L computed purely from ledger rows."""
        out = defaultdict(float)
        pending: dict[str, float] = {}
        for e in events:
            if e.type is not EventType.FINANCIAL_EVENT:
                continue
            p = e.payload
            amt = float(p["amount"])
            if p["status"] == "unrealized":
                pending[p["external_reference"]] = amt if p["type"] == "revenue" else -amt
                continue
            if p.get("settles_reference"):
                pending.pop(p["settles_reference"], None)
            if p["type"] == "revenue":
                out["gross_revenue"] += amt
            elif p["type"] == "refund":
                out["refunds"] += amt
            elif p["type"] == "fee":
                out["fees"] += amt
            else:
                out[p["category"]] += amt
        costs = out["fees"] + sum(out[c] for c in EXPENSE_CATEGORIES)
        out["total_costs"] = costs
        out["net_realized_profit"] = out["gross_revenue"] - out["refunds"] - costs
        out["unrealized"] = sum(pending.values())
        return {k: round(v, 6) for k, v in out.items()}
