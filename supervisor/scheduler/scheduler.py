"""Evidence-weighted inference scheduler (DESIGN §7).

    priority = exploitation_score
             + exploration_bonus
             + starvation_bonus
             - lineage_concentration_penalty
             - marginal_resource_cost

Slots each tick are split into a randomized equal-allocation control pool
(DESIGN §9.3), a performance-directed exploitation pool and a protected
exploration pool. Per-agent, per-lineage and burst caps bound concentration;
a starvation guarantee bounds waiting. Every decision records its reasons.
"""

from __future__ import annotations

import math
import random
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Sequence

from storage.events.canonical import digest

from ..evaluator.stats import shrink

METHODS = ("thompson", "ucb", "softmax", "equal")


@dataclass
class Candidate:
    agent_id: str
    lineage_id: str
    steps: int
    net: float
    step_values: list[float]
    gpu_per_step: float
    last_scheduled_tick: int | None
    burst: int
    cohort: str = "treatment"
    prior_mean: float = 0.0
    generation_start_tick: int = 0


@dataclass
class Selected:
    agent_id: str
    lineage_id: str
    pool: str
    priority: float
    components: dict[str, float]

    def to_dict(self) -> dict[str, Any]:
        return {"agent_id": self.agent_id, "lineage_id": self.lineage_id, "pool": self.pool,
                "priority": round(self.priority, 6), "components": {k: round(v, 6) for k, v in self.components.items()}}


@dataclass
class AllocationDecision:
    tick: int
    method: str
    slots: int
    selected: list[Selected]
    priorities: dict[str, float]
    pools: dict[str, int]
    starving: list[str] = field(default_factory=list)
    cap_blocked: list[str] = field(default_factory=list)
    candidates: int = 0
    reason: str | None = None

    @property
    def unfilled(self) -> int:
        return max(0, self.slots - len(self.selected))

    def to_payload(self) -> dict[str, Any]:
        return {
            "tick": self.tick, "method": self.method, "slots": self.slots,
            "selected": [s.to_dict() for s in self.selected], "unfilled": self.unfilled,
            "candidates": self.candidates, "queue_depth": max(0, self.candidates - len(self.selected)),
            "priorities": {k: round(v, 6) for k, v in sorted(self.priorities.items())},
            "pools": self.pools, "starving": self.starving, "cap_blocked": sorted(set(self.cap_blocked)),
            "reason": self.reason,
        }


class Scheduler:
    def __init__(self, config: dict[str, Any], *, seed: int, min_exposure_steps: int,
                 variance_floor: float = 0.25) -> None:
        self.cfg = config
        self.seed = seed
        self.method = config.get("method", "thompson")
        if self.method not in METHODS:
            raise ValueError(f"unknown scheduler method {self.method!r}")
        self.exploration_share = float(config.get("exploration_share", 0.3))
        self.max_agent_share = float(config.get("max_agent_share", 1.0))
        self.max_lineage_share = float(config.get("max_lineage_share", 1.0))
        self.cap_window = int(config.get("cap_window_ticks", 36))
        self.max_burst = int(config.get("max_burst_ticks", 10**9))
        self.starvation_ticks = max(1, int(config.get("starvation_ticks", 18)))
        self.w_starve = float(config.get("starvation_weight", 1.0))
        self.w_explore = float(config.get("exploration_weight", 1.0))
        self.w_lineage = float(config.get("lineage_penalty_weight", 1.0))
        self.w_cost = float(config.get("resource_cost_weight", 0.1))
        self.temperature = float(config.get("softmax_temperature", 0.25))
        self.prior_strength = float(config.get("prior_strength", 5))
        # Markets drift within a generation; recent steps count for more so an
        # early lucky streak decays instead of holding priority indefinitely.
        self.half_life = config.get("evidence_half_life_steps")
        self.min_exposure = min_exposure_steps
        self.variance_floor = variance_floor

    # ---------------------------------------------------------------- main
    def allocate(self, tick: int, generation: int, candidates: Sequence[Candidate], slots: int,
                 history: Sequence[dict[str, Any]], *, reason: str | None = None) -> AllocationDecision:
        rng = random.Random(int(digest([self.seed, "scheduler", generation, tick])[:16], 16))
        decision = AllocationDecision(tick=tick, method=self.method, slots=slots, selected=[], priorities={},
                                      pools={"control": 0, "exploit": 0, "explore": 0},
                                      candidates=len(candidates), reason=reason)
        if not candidates or slots <= 0:
            return decision
        comps = self._components(tick, candidates, history, rng)
        decision.priorities = {a: sum(v for v in comp.values()) for a, comp in comps.items()}

        recent = [h for h in history if h["tick"] > tick - self.cap_window]
        agent_counts = Counter(a for h in recent for a in h["agents"])
        lineage_counts = Counter(lin for h in recent for lin in h["lineages"])
        window_total = sum(len(h["agents"]) for h in recent)
        chosen: dict[str, Selected] = {}

        def allowed(c: Candidate, *, lineage_cap: bool = True) -> bool:
            if c.agent_id in chosen:
                return False
            if c.last_scheduled_tick == tick - 1 and c.burst >= self.max_burst:
                decision.cap_blocked.append(c.agent_id)
                return False
            total = window_total + len(chosen) + 1
            # Caps only bind once the window holds enough allocations to make
            # a share meaningful; otherwise one tick would block everything.
            if total >= 1 / min(self.max_agent_share, 1.0) * 2:
                if (agent_counts[c.agent_id] + 1) / total > self.max_agent_share:
                    decision.cap_blocked.append(c.agent_id)
                    return False
            if lineage_cap and total >= 1 / min(self.max_lineage_share, 1.0) * 2:
                if (lineage_counts[c.lineage_id] + 1) / total > self.max_lineage_share:
                    decision.cap_blocked.append(c.agent_id)
                    return False
            return True

        def take(c: Candidate, pool: str) -> None:
            chosen[c.agent_id] = Selected(c.agent_id, c.lineage_id, pool, decision.priorities[c.agent_id],
                                          comps[c.agent_id])
            lineage_counts[c.lineage_id] += 1
            decision.pools[pool] = decision.pools.get(pool, 0) + 1

        slots = min(slots, len(candidates))

        if self.method == "equal":
            for c in self._round_robin(candidates, rng, agent_counts):
                if len(chosen) >= slots:
                    break
                if allowed(c, lineage_cap=False):
                    take(c, "equal")
            decision.selected = list(chosen.values())
            return decision

        control = [c for c in candidates if c.cohort == "control"]
        treatment = [c for c in candidates if c.cohort != "control"]
        control_slots = min(len(control), round(slots * len(control) / len(candidates)))
        for c in self._round_robin(control, rng, agent_counts):
            if decision.pools["control"] >= control_slots:
                break
            if allowed(c, lineage_cap=False):
                take(c, "control")

        remaining = slots - len(chosen)
        n_explore = min(remaining, max(1 if remaining and self.exploration_share > 0 else 0,
                                       round(remaining * self.exploration_share)))
        n_exploit = remaining - n_explore

        # Starvation guarantee first, inside the exploration pool.
        starving = sorted(
            (c for c in treatment if self._waited(c, tick) > self.starvation_ticks),
            key=lambda c: (-self._waited(c, tick), c.agent_id),
        )
        decision.starving = [c.agent_id for c in starving]
        explore_taken = 0
        for c in starving:
            if explore_taken >= n_explore:
                break
            if allowed(c, lineage_cap=False):
                take(c, "explore")
                explore_taken += 1

        # Exploitation: evidence-weighted priority.
        pool = [c for c in treatment if c.agent_id not in chosen]
        if self.method == "softmax":
            order = self._softmax_order(pool, decision.priorities, rng)
        else:
            order = sorted(pool, key=lambda c: (-decision.priorities[c.agent_id], c.agent_id))
        exploit_taken = 0
        for c in order:
            if exploit_taken >= n_exploit:
                break
            if allowed(c):
                take(c, "exploit")
                exploit_taken += 1

        # Protected exploration: favours unexposed / uncertain agents.
        pool = [c for c in treatment if c.agent_id not in chosen]
        weights = {c.agent_id: (3.0 if c.steps < self.min_exposure else 1.0) / (1 + c.steps) for c in pool}
        for c in self._weighted_order(pool, weights, rng):
            if explore_taken >= n_explore:
                break
            if allowed(c):
                take(c, "explore")
                explore_taken += 1

        # Unused exploitation capacity (caps) flows to exploration and back.
        for c in sorted((c for c in treatment if c.agent_id not in chosen),
                        key=lambda c: (-decision.priorities[c.agent_id], c.agent_id)):
            if len(chosen) >= slots:
                break
            if allowed(c):
                take(c, "explore")

        decision.selected = list(chosen.values())
        return decision

    # ------------------------------------------------------------ helpers
    def _waited(self, c: Candidate, tick: int) -> int:
        last = c.last_scheduled_tick if c.last_scheduled_tick is not None else c.generation_start_tick - 1
        return tick - last

    def _components(self, tick: int, candidates: Sequence[Candidate], history: Sequence[dict[str, Any]],
                    rng: random.Random) -> dict[str, dict[str, float]]:
        est = {}
        for c in candidates:
            total, n, values = self._evidence(c)
            post = shrink(total, n, values, prior_mean=c.prior_mean,
                          prior_strength=self.prior_strength, variance_floor=self.variance_floor)
            rate = 3600.0 / max(c.gpu_per_step, 1e-6)
            est[c.agent_id] = (post.mean * rate, post.sd * rate)
        means = [m for m, _ in est.values()]
        lo, hi = min(means), max(means)
        span = max(hi - lo, max(abs(hi), abs(lo), 1e-9) * 0.1, 1e-9)
        avg_gpu = sum(c.gpu_per_step for c in candidates) / len(candidates)
        recent = [h for h in history if h["tick"] > tick - self.cap_window]
        lineage_counts = Counter(lin for h in recent for lin in h["lineages"])
        window_total = sum(len(h["agents"]) for h in recent) or 1

        out: dict[str, dict[str, float]] = {}
        for c in sorted(candidates, key=lambda c: c.agent_id):
            m, sd = est[c.agent_id]
            if self.method == "thompson":
                exploit = (rng.gauss(m, sd) - lo) / span
                explore = 0.0
            else:
                exploit = (m - lo) / span
                explore = self.w_explore * sd / span if self.method == "ucb" else 0.0
            waited = self._waited(c, tick)
            starvation = self.w_starve * max(0, waited - self.starvation_ticks) / self.starvation_ticks
            share = lineage_counts[c.lineage_id] / window_total
            lineage_pen = self.w_lineage * max(0.0, share - self.max_lineage_share) / max(self.max_lineage_share, 1e-9)
            cost = self.w_cost * (c.gpu_per_step / avg_gpu if avg_gpu > 0 else 1.0)
            out[c.agent_id] = {
                "exploitation_score": exploit,
                "exploration_bonus": explore,
                "starvation_bonus": starvation,
                "lineage_concentration_penalty": -lineage_pen or 0.0,
                "marginal_resource_cost": -cost,
            }
        return out

    def _evidence(self, c: Candidate) -> tuple[float, float, list[float]]:
        """(weighted outcome total, effective sample size, values for variance)."""
        if not self.half_life or not c.step_values:
            return c.net, c.steps, c.step_values
        hl = float(self.half_life)
        n = len(c.step_values)
        weights = [0.5 ** ((n - 1 - i) / hl) for i in range(n)]
        total = sum(w * v for w, v in zip(weights, c.step_values))
        return total, sum(weights), c.step_values[-int(3 * hl):]

    @staticmethod
    def _round_robin(cands: Sequence[Candidate], rng: random.Random, recent: Counter) -> list[Candidate]:
        """Fewest recent allocations first, then longest wait, then a seeded coin."""
        keyed = [(recent[c.agent_id], c.last_scheduled_tick if c.last_scheduled_tick is not None else -1,
                  rng.random(), c) for c in cands]
        return [k[-1] for k in sorted(keyed, key=lambda t: t[:3])]

    def _softmax_order(self, pool: Sequence[Candidate], priorities: dict[str, float],
                       rng: random.Random) -> list[Candidate]:
        if not pool:
            return []
        top = max(priorities[c.agent_id] for c in pool)
        weights = {c.agent_id: math.exp((priorities[c.agent_id] - top) / max(self.temperature, 1e-6)) for c in pool}
        return self._weighted_order(pool, weights, rng)

    @staticmethod
    def _weighted_order(pool: Sequence[Candidate], weights: dict[str, float], rng: random.Random) -> list[Candidate]:
        """Weighted sampling without replacement (Efraimidis-Spirakis keys)."""
        keyed = []
        for c in sorted(pool, key=lambda c: c.agent_id):
            w = max(weights.get(c.agent_id, 0.0), 1e-12)
            keyed.append((rng.random() ** (1.0 / w), c))
        return [c for _, c in sorted(keyed, key=lambda t: -t[0])]
