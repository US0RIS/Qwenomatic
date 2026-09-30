"""Generation selection (DESIGN §10.2).

Not a deterministic "top N clone, bottom M die" rule:
- preserve a small elite set, ranked by the lower confidence bound;
- retire probabilistically from the lowest upper confidence bounds (agents
  that are confidently weak), only after minimum exposure;
- choose parents probabilistically, weighted by posterior-mean fitness;
- keep a minimum number of lineages alive;
- cap how much of the next population one lineage may hold, relaxed only
  when the evidence for that lineage is strong.
"""

from __future__ import annotations

import math
import random
from collections import Counter
from typing import Any


def _softmax_weights(scores: list[float], temperature: float) -> list[float]:
    if not scores:
        return []
    mu = sum(scores) / len(scores)
    sd = math.sqrt(sum((s - mu) ** 2 for s in scores) / len(scores)) or 1.0
    z = [(s - mu) / sd for s in scores]
    top = max(z)
    return [math.exp((v - top) / max(temperature, 1e-6)) for v in z]


def plan_selection(
    results: dict[str, dict[str, Any]],
    lineage_of: dict[str, str],
    *,
    population_size: int,
    cfg: dict[str, Any],
    rng: random.Random,
) -> dict[str, Any]:
    """Pure function of (fitness results, config, rng). Replayable (I9)."""
    ids = sorted(results)
    disq = [a for a in ids if not results[a]["eligible"]]
    health = [a for a in ids if results[a]["eligible"] and results[a]["health_failed"]]
    exposed = [a for a in ids if results[a]["eligible"] and not results[a]["health_failed"]
               and results[a]["min_exposure_met"]]
    protected = [a for a in ids if results[a]["eligible"] and not results[a]["health_failed"]
                 and not results[a]["min_exposure_met"]]

    ranked = sorted(exposed, key=lambda a: (-results[a]["fitness"], a))
    elites = sorted(exposed, key=lambda a: (-results[a]["fitness_lcb"], a))[: int(cfg.get("elite_count", 2))]

    # Probabilistic retirement: the lower an agent's upper bound, the likelier
    # it retires. An uncertain agent that might be good is not culled early.
    retire_n = round(float(cfg.get("retire_fraction", 0.3)) * len(exposed))
    pool = sorted((a for a in exposed if a not in elites), key=lambda a: (-results[a]["fitness_ucb"], a))
    keyed = [(rng.random() ** (1.0 / ((i + 1) ** 2)), a) for i, a in enumerate(pool)]
    order = [a for _, a in sorted(keyed, key=lambda t: -t[0])]
    min_lineages = int(cfg.get("min_lineages", 1))
    survivors_pre = set(exposed) | set(protected)
    retired_by_selection: list[str] = []
    for a in order:
        if len(retired_by_selection) >= retire_n:
            break
        remaining = survivors_pre - set(retired_by_selection) - {a}
        lineages_left = {lineage_of[x] for x in remaining}
        if lineage_of[a] not in lineages_left and len(lineages_left) < min_lineages:
            continue  # diversity protection: last member of a lineage
        retired_by_selection.append(a)

    survivors = sorted(survivors_pre - set(retired_by_selection))
    # Never exceed the configured population (e.g. after a config change).
    overflow = len(survivors) - population_size
    if overflow > 0:
        bottom = sorted(survivors, key=lambda a: (results[a]["fitness_ucb"] or -math.inf, a))[:overflow]
        retired_by_selection += bottom
        survivors = sorted(set(survivors) - set(bottom))

    retirements = (
        [{"agent_id": a, "reason": "disqualified: " + "; ".join(results[a]["eligibility_reasons"])} for a in disq]
        + [{"agent_id": a, "reason": "health check failed"} for a in health]
        + [{"agent_id": a, "reason": f"selection: fitness rank {ranked.index(a) + 1} of {len(ranked)}, "
                                     f"upper bound {results[a]['fitness_ucb']:.2f}"
            if a in ranked else "selection: population cap"} for a in retired_by_selection]
    )

    # Lineage cap, relaxed only when one lineage clearly leads.
    by_lineage: dict[str, list[str]] = {}
    for a in exposed:
        by_lineage.setdefault(lineage_of[a], []).append(a)
    best = sorted(
        ((max(results[a]["fitness_lcb"] for a in members), max(results[a]["fitness_ucb"] for a in members), lin)
         for lin, members in by_lineage.items()),
        reverse=True,
    )
    strong = len(best) >= 2 and best[0][0] > max(b[1] for b in best[1:])
    cap_share = float(cfg.get("max_lineage_share_strong_evidence" if strong else "max_lineage_share", 1.0))
    cap = max(1, math.floor(cap_share * population_size))

    slots = max(0, population_size - len(survivors))
    immigrants = min(slots, round(slots * float(cfg.get("immigration_rate", 0.0))))
    parents_pool = ranked
    weights = _softmax_weights([results[a]["fitness"] for a in parents_pool],
                               float(cfg.get("parent_temperature", 0.5)))
    counts = Counter(lineage_of[a] for a in survivors)
    offspring: list[dict[str, Any]] = []
    for slot in range(slots - immigrants):
        eligible = [(a, w) for a, w in zip(parents_pool, weights) if counts[lineage_of[a]] < cap]
        if not eligible:
            offspring.append({"slot": slot, "origin": "immigrant", "parent_id": None, "reason": "lineage caps"})
            continue
        parent = rng.choices([a for a, _ in eligible], weights=[w for _, w in eligible])[0]
        counts[lineage_of[parent]] += 1
        offspring.append({"slot": slot, "origin": "offspring", "parent_id": parent,
                          "lineage_id": lineage_of[parent]})
    for i in range(immigrants):
        offspring.append({"slot": slots - immigrants + i, "origin": "immigrant", "parent_id": None,
                          "reason": "exploration"})

    return {
        "elites": elites,
        "ranked": ranked,
        "protected": protected,
        "survivors": survivors,
        "retirements": retirements,
        "offspring": offspring,
        "lineage_cap": cap,
        "strong_evidence": strong,
        "parent_weights": {a: round(w, 6) for a, w in zip(parents_pool, weights)},
    }
