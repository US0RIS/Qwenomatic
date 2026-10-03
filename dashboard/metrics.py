"""Dashboard views, reconstructed from the ledger alone (DESIGN §14).

Nothing here reads supervisor memory: every number is derived from events,
so the dashboard can be checked against an independent replay.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Any

from storage.events import Event, EventStore, EventType, FarmState

from supervisor.accounting.ledger import Ledger
from supervisor.evolution.lineage import lineage_tree


class LedgerView:
    """Incrementally maintained projection over a (read-only) store."""

    def __init__(self, store: EventStore) -> None:
        self.store = store
        self.state = FarmState(cap_window=512)
        self._pnl_events: list[Event] = []

    def refresh(self) -> None:
        for e in self.store.iter_events(after_seq=self.state.last_seq):
            self.state.apply(e)
            if e.type is EventType.FINANCIAL_EVENT:
                self._pnl_events.append(e)

    # ------------------------------------------------------------- views
    def overview(self) -> dict[str, Any]:
        s = self.state
        pnl = Ledger.pnl(self._pnl_events)
        gen = s.generations.get(s.current_generation) if s.current_generation is not None else None
        statuses = Counter(a.status for a in s.agents.values())
        last = s.allocations[-1] if s.allocations else None
        last_alloc = self._last_allocation()
        tokens = sum(c.tokens for g in s.counters.values() for c in g.values())
        gpu = sum(c.gpu_seconds for g in s.counters.values() for c in g.values())
        return {
            "market": self.market_evidence(),
            "generation": s.current_generation,
            "generation_started_at": gen.started_at if gen else None,
            "generation_ends_at": gen.ends_at if gen else None,
            "time_remaining_hours": self._remaining_hours(gen),
            "population": sum(1 for a in s.agents.values() if a.active),
            "status_counts": dict(statuses),
            "role_counts": dict(Counter(a.role for a in s.agents.values() if a.active)),
            "role_targets": gen.config.get('roles', {}).get('counts', {}) if gen else {},
            "halted": s.halted,
            "halt_reason": s.halt_reason,
            "tick": s.last_tick,
            "queue_depth": last_alloc["queue_depth"] if last_alloc else 0,
            "scheduled_last_tick": len(last["agents"]) if last else 0,
            "gpu_utilization": self._gpu_utilization(),
            "gross_revenue": pnl.get("gross_revenue", 0.0),
            "refunds": pnl.get("refunds", 0.0),
            "net_realized_profit": pnl.get("net_realized_profit", 0.0),
            "unrealized": pnl.get("unrealized", 0.0),
            "external_spend": pnl.get("external_spend", 0.0),
            "total_costs": pnl.get("total_costs", 0.0),
            "compute_imputed": pnl.get("compute_imputed", 0.0),
            "inference_tokens": tokens,
            "gpu_seconds": round(gpu, 3),
            "human_interventions": s.human_interventions,
            "policy_violations": s.violations_total,
            "daily_pnl": self.daily_pnl(),
            "events": s.last_seq,
        }

    def market_evidence(self):
        configs = self.store.iter_events(types=[EventType.MARKET_CONFIGURED], limit=1)
        sessions = self.store.iter_events(types=[EventType.SUPERVISOR_STARTED], limit=1, descending=True)
        config = configs[0].payload if configs else {}
        backend = sessions[0].payload.get('backend', 'unknown') if sessions else 'unknown'
        model = config.get('model', 'toy_v1 (legacy ledger)')
        pending = list(self.state.pending_settlements.values())
        initial = config.get('config', {}).get('realism', {}).get('initial_cash')
        pnl = Ledger.pnl(self._pnl_events)
        return {'model': model, 'backend': backend, 'simulated_economy': True,
                'decision_maker': 'Scripted policy emulator; Qwen is not running' if backend == 'simulated' else 'Model inference; synthetic customers and assumed delivery quality',
                'real_world_validity': 'unestablished',
                'evidence_status': config.get('evidence_status', 'uncalibrated toy assumptions'),
                'initial_capital': initial,
                'simulated_cash': round(initial+pnl['net_realized_profit']+pnl['compute_imputed'], 6) if initial is not None else None,
                'pending_payments': sum(p['amount'] for p in pending if p.get('kind') == 'sale'),
                'pending_refunds': sum(p['amount'] for p in pending if p.get('kind') == 'refund'),
                'note': 'Simulated dollars. Payment and refund tails may remain. This does not validate a business.'}

    def _last_allocation(self) -> dict[str, Any] | None:
        rows = self.store.iter_events(types=[EventType.SCHEDULER_ALLOCATION], limit=1, descending=True)
        return rows[0].payload | {"recorded_at": rows[0].recorded_at} if rows else None

    def _remaining_hours(self, gen: Any) -> float | None:
        """Generation time left, measured on the farm's own clock (the latest ledger timestamp)."""
        latest = self.store.get(self.state.last_seq) if self.state.last_seq else None
        if not gen or latest is None:
            return None
        now = datetime.fromisoformat(latest.recorded_at)
        return round((datetime.fromisoformat(gen.ends_at) - now).total_seconds() / 3600, 3)

    def _gpu_utilization(self, window: int = 36) -> float | None:
        """GPU-seconds used over the last `window` ticks / GPU-seconds available."""
        s = self.state
        gen = s.generations.get(s.current_generation) if s.current_generation is not None else None
        if s.last_tick < 0 or gen is None:
            return None
        timing = gen.config.get("timing", {})
        tick_seconds, gpus = float(timing.get("tick_seconds", 0)), int(timing.get("gpu_count", 1))
        if tick_seconds <= 0:
            return None
        ticks = range(max(0, s.last_tick - window + 1), s.last_tick + 1)
        used = sum(s.gpu_by_tick.get(t, 0.0) for t in ticks)
        return round(used / (len(ticks) * tick_seconds * gpus), 6)

    def daily_pnl(self) -> list[dict[str, Any]]:
        days: dict[str, dict[str, float]] = {}
        for e in self._pnl_events:
            p = e.payload
            if p["status"] != "realized":
                continue
            d = days.setdefault(e.recorded_at[:10], {"gross": 0.0, "costs": 0.0, "refunds": 0.0})
            amt = float(p["amount"])
            if p["type"] == "revenue":
                d["gross"] += amt
            elif p["type"] == "refund":
                d["refunds"] += amt
            else:
                d["costs"] += amt
        return [{"day": k, "gross": round(v["gross"], 2), "net": round(v["gross"] - v["refunds"] - v["costs"], 2)}
                for k, v in sorted(days.items())]

    def population(self) -> list[dict[str, Any]]:
        s = self.state
        gen = s.current_generation
        rows = []
        for a in s.agents.values():
            if not a.active and a.status != "disqualified":
                continue
            c = s.counters.get(gen, {}).get(a.id) if gen is not None else None
            prev = s.fitness.get((gen or 0) - 1, {}).get(a.id, {})
            rows.append({
                "agent_id": a.id, "lineage_id": a.lineage_id, "status": a.status, "status_reason": a.status_reason,
                "origin": a.origin, "age": (gen or 0) - a.generation_born,
                "role": a.role, "role_slot": a.role_slot, "role_metric": prev.get('metric'),
                "segment": a.genotype["target"]["segment"] if a.role == 'business' else None,
                "price": a.genotype["pricing_parameters"]["price"] if a.role == 'business' else None,
                "workflow": a.genotype["workflow"],
                "cohort": s.generations[gen].cohort.get(a.id) if gen is not None else None,
                "last_fitness": prev.get("fitness"), "last_fitness_sd": prev.get("fitness_sd"),
                "research_results": [e.payload for e in self.store.iter_events(types=[EventType.RESEARCH_RESULT], agent_id=a.id)][-2:],
                "red_team_cases": len(self.store.iter_events(types=[EventType.RED_TEAM_FINDING], agent_id=a.id)),
                "latest_mutation": a.mutations[-1] if a.mutations else None,
                **({k: v for k, v in c.to_dict().items()} if c else {}),
            })
        return sorted(rows, key=lambda r: -(r.get("net_realized") or 0))

    def scheduler(self) -> dict[str, Any]:
        rows = self.store.iter_events(types=[EventType.SCHEDULER_ALLOCATION], limit=36, descending=True)
        latest = rows[0].payload if rows else None
        pool_totals: Counter[str] = Counter()
        agent_share: Counter[str] = Counter()
        lineage_share: Counter[str] = Counter()
        for r in rows:
            for sel in r.payload["selected"]:
                pool_totals[sel["pool"]] += 1
                agent_share[sel["agent_id"]] += 1
                lineage_share[sel["lineage_id"]] += 1
        total = sum(agent_share.values()) or 1
        return {
            "latest": latest,
            "window_ticks": len(rows),
            "pool_totals": dict(pool_totals),
            "agent_shares": {k: round(v / total, 4) for k, v in agent_share.most_common()},
            "lineage_shares": {k: round(v / total, 4) for k, v in lineage_share.most_common()},
            "starvation_warnings": latest["starving"] if latest else [],
            "cap_blocked": latest["cap_blocked"] if latest else [],
        }

    def evolution(self) -> dict[str, Any]:
        s = self.state
        reports = {e.generation_id: e.payload for e in self.store.iter_events(types=[EventType.ATTRIBUTION_REPORT])}
        return {
            "tree": lineage_tree(s),
            "selections": {g: {k: v for k, v in sel["plan"].items() if k != "parent_weights"}
                           for g, sel in sorted(s.selections.items())},
            "attribution": reports,
            "generations": [{"number": g.number, "started_at": g.started_at, "ends_at": g.ends_at,
                             "closed": g.closed, "diversity": g.close_steps.get("activate_next_population", {}).get("diversity")}
                            for g in sorted(s.generations.values(), key=lambda g: g.number)],
        }

    def audit(self, *, type: str | None = None, agent_id: str | None = None, before: int | None = None,
              limit: int = 200) -> list[dict[str, Any]]:
        types = [EventType(type)] if type else None
        rows = self.store.iter_events(types=types, agent_id=agent_id, limit=limit, descending=True,
                                      upto_seq=(before - 1) if before else None)
        return [e.to_dict() for e in rows]

    def policy(self) -> dict[str, Any]:
        s = self.state
        recent = self.store.iter_events(types=[EventType.POLICY_DECISION], limit=200, descending=True)
        violations = self.store.iter_events(types=[EventType.POLICY_VIOLATION], limit=200, descending=True)
        gen = s.current_generation
        budgets = []
        for a in s.agents.values():
            if not a.active or gen is None:
                continue
            c = s.counters.get(gen, {}).get(a.id)
            if c is None:
                continue
            budgets.append({"agent_id": a.id, "tokens": c.tokens, "token_budget": a.budgets.get("inference_tokens"),
                            "gpu_seconds": round(c.gpu_seconds, 2), "gpu_budget": a.budgets.get("gpu_seconds"),
                            "external_spend": round(c.external_spend, 2), "spend_budget": a.budgets.get("external_spend"),
                            "tool_calls": c.tool_calls, "tool_call_budget": a.budgets.get("tool_calls")})
        return {
            "decision_counts": dict(s.policy_decisions),
            "recent_non_allow": [e.to_dict() for e in recent if e.payload["decision"] != "allow"][:50],
            "violations": [e.to_dict() for e in violations],
            "approvals": sorted(s.approvals.values(), key=lambda a: a["requested_at"], reverse=True),
            "budgets": budgets,
        }
