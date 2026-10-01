"""Audit and replay (DESIGN §16 items 9 and 10, invariant I9).

Every headline claim must be recomputable from the ledger alone.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from storage.events import EventStore, EventType, FarmState

from .accounting import Ledger
from .evolution.generation import evaluate_generation, selection_for, snapshot_state

if TYPE_CHECKING:  # pragma: no cover
    from .core import Supervisor


def replay_accounting(store: EventStore) -> dict[str, Any]:
    """Recompute P&L three ways and check they agree."""
    events = store.iter_events()
    pnl_a = Ledger.pnl(events)
    pnl_b = Ledger.pnl(store.iter_events())
    state = FarmState().replay(events)
    gross = sum(c.gross_revenue for g in state.counters.values() for c in g.values())
    net = sum(c.net_realized for g in state.counters.values() for c in g.values())
    balance = Ledger.trial_balance(events)
    ok = (
        pnl_a == pnl_b
        and abs(gross - pnl_a["gross_revenue"]) < 1e-6
        and abs(net - pnl_a["net_realized_profit"]) < 1e-6
        and abs(sum(balance.values())) < 1e-6
    )
    return {"ok": ok, "pnl": pnl_a, "projection_gross": round(gross, 6), "projection_net": round(net, 6),
            "trial_balance_sum": round(sum(balance.values()), 9)}


def replay_selection(sup: "Supervisor", generation: int) -> dict[str, Any]:
    """Recompute fitness and selection from the frozen snapshot; compare with the ledger."""
    recorded = sup.state.selections.get(generation)
    if recorded is None:
        return {"ok": False, "reason": "no selection recorded"}
    state = snapshot_state(sup, recorded["snapshot_seq"])
    results = evaluate_generation(sup, state, generation)
    recorded_fitness = sup.state.fitness.get(generation, {})
    fitness_ok = results == recorded_fitness
    plan = selection_for(sup, state, generation, results)
    return {"ok": fitness_ok and plan == recorded["plan"], "fitness_match": fitness_ok,
            "plan_match": plan == recorded["plan"], "snapshot_seq": recorded["snapshot_seq"]}


def verify(sup: "Supervisor") -> dict[str, Any]:
    chain_ok, bad = sup.store.verify_chain()
    selections = {g: replay_selection(sup, g)["ok"] for g in sorted(sup.state.selections)}
    started = [e.payload["number"] for e in sup.store.iter_events(types=[EventType.GENERATION_STARTED])]
    return {
        "hash_chain": {"ok": chain_ok, "first_bad_seq": bad},
        "accounting": replay_accounting(sup.store),
        "selection_replay": selections,
        "generations_started_once": len(started) == len(set(started)),
    }
