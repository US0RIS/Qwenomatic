"""DESIGN §16 items 1, 2, 3 and 6, plus adaptive vs equal scheduling (Phase 3).

Ground truth is known because the economy is simulated. These tests check the
evolutionary mechanism, not any claim about real revenue.
"""

from collections import Counter

import pytest

from helpers import genotype, make_supervisor, run_generations
from storage.events import EventType
from supervisor.ids import stable_id

DAY = {"generation": {"duration_hours": 24}}


def population(sup, g):
    return [sup.state.agents[a] for a in sup.state.generations[g].cohort]


def net_per_step(sup, g):
    cs = sup.state.counters[g].values()
    return sum(c.net_realized for c in cs) / sum(c.steps for c in cs)


# ------------------------------------------------------------------ §16.1
def test_known_optimum_attracts_the_population(tmp_path):
    """A deliberately superior segment exists; the farm should converge toward it."""
    overrides = {"farm": {**DAY, "simulation": {"market": {"segments": {"smb-bookkeeping": {"base_conversion": 0.35}}}}}}
    sup = make_supervisor(tmp_path, overrides, segments=["freelancer-templates", "smb-bookkeeping", "local-services"])
    run_generations(sup, 5)
    on_optimum = [sum(a.genotype["target"]["segment"] == "smb-bookkeeping" for a in population(sup, g))
                  for g in range(6)]
    assert on_optimum[5] >= 12, on_optimum
    assert on_optimum[5] > on_optimum[0] + 4, on_optimum
    early, late = net_per_step(sup, 0), (net_per_step(sup, 3) + net_per_step(sup, 4)) / 2
    assert late > 1.5 * early, (early, late)


# ------------------------------------------------------------------ §16.2
def test_deceptive_early_reward_does_not_monopolize_compute(tmp_path):
    seeds = [genotype("trend-hype", 30.0)] * 10 + [genotype("freelancer-templates", 20.0)] * 10
    sup = make_supervisor(tmp_path, {"farm": DAY}, segments=["trend-hype", "freelancer-templates"],
                          seed_genotypes=seeds)
    run_generations(sup, 3)
    allocs = sup.store.iter_events(types=[EventType.SCHEDULER_ALLOCATION])

    def hype_share(lo, hi):
        sel = [s for e in allocs if lo <= e.payload["tick"] < hi for s in e.payload["selected"] if s["pool"] == "exploit"]
        return sum(sup.state.agents[s["agent_id"]].genotype["target"]["segment"] == "trend-hype" for s in sel) / len(sel)

    ticks = sup.config.ticks_per_generation
    early, late, next_gen = hype_share(0, ticks // 6), hype_share(2 * ticks // 3, ticks), hype_share(ticks, 2 * ticks)
    assert late <= 0.75 * early, (early, late)
    assert next_gen < 0.15, next_gen
    hype_agents = [sum(a.genotype["target"]["segment"] == "trend-hype" for a in population(sup, g)) for g in range(4)]
    assert hype_agents[3] < hype_agents[0], hype_agents
    later = hype_share(2 * ticks, 3 * ticks)
    assert later < 0.15, later


# ------------------------------------------------------------------ §16.3
def _delayed_farm(tmp_path, fitness_overrides=None):
    overrides = {"farm": {**DAY, "simulation": {"market": {"segments": {"enterprise-audit": {"base_conversion": 0.06}}}}}}
    if fitness_overrides:
        overrides["fitness"] = fitness_overrides
    seeds = [genotype("enterprise-audit", 500.0)] * 5 + [genotype("freelancer-templates", 20.0)] * 15
    sup = make_supervisor(tmp_path, overrides, segments=["enterprise-audit", "freelancer-templates"],
                          seed_genotypes=seeds)
    return sup, [stable_id(sup.config.seed, "agent", 0, i) for i in range(5)]


def test_delayed_reward_strategies_survive_until_evaluated(tmp_path):
    sup, enterprise = _delayed_farm(tmp_path)
    run_generations(sup, 3)
    for g in (0, 1):
        retired = {r["agent_id"] for r in sup.state.selections[g]["plan"]["retirements"]}
        assert not retired & set(enterprise), f"long-cycle agents culled at generation {g}"
        # They lost money in-window: survival comes from the archetype window, not luck.
        assert all(sup.state.fitness[g][a]["min_exposure_met"] is False for a in enterprise)
    # By generation 2 they are evaluated over a three-generation window that includes settled revenue.
    assert all(sup.state.fitness[2][a]["window_generations"] == [0, 1, 2] for a in enterprise)
    gross = sum(sup.state.counters[g][a].gross_revenue for g in range(3) for a in enterprise)
    assert gross > 0


def test_without_archetype_windows_long_cycle_agents_die_young(tmp_path):
    sup, enterprise = _delayed_farm(tmp_path, {"archetypes": {"long_cycle": {"window_generations": 1,
                                                                              "min_age_generations": 0}}})
    run_generations(sup, 1)
    retired = {r["agent_id"] for r in sup.state.selections[0]["plan"]["retirements"]}
    assert retired & set(enterprise)


# ------------------------------------------------------------------ §16.6
def test_lineage_takeover_is_capped(tmp_path):
    overrides = {"farm": {**DAY, "simulation": {"market": {"segments": {"smb-bookkeeping": {"base_conversion": 0.35}}}}}}
    seeds = [genotype("smb-bookkeeping", 60.0)] + [genotype("local-services", 40.0)] * 19
    sup = make_supervisor(tmp_path, overrides, segments=["smb-bookkeeping", "local-services"], seed_genotypes=seeds)
    run_generations(sup, 4)
    evo = sup.config.farm["evolution"]
    size = sup.config.population_size
    for g in range(1, 5):
        counts = Counter(a.lineage_id for a in population(sup, g))
        plan = sup.state.selections[g - 1]["plan"]
        share = evo["max_lineage_share_strong_evidence"] if plan["strong_evidence"] else evo["max_lineage_share"]
        assert max(counts.values()) <= int(share * size), (g, counts.most_common(2))
        assert len(counts) >= evo["min_lineages"]
    # Scheduler lineage share over full cap windows respects the cap.
    cfg = sup.config.farm["scheduler"]
    allocs = sup.store.iter_events(types=[EventType.SCHEDULER_ALLOCATION])
    w = cfg["cap_window_ticks"]
    for i in range(w, len(allocs), w):
        window = [s["lineage_id"] for e in allocs[i - w:i] for s in e.payload["selected"] if s["pool"] != "control"]
        if window:
            top = Counter(window).most_common(1)[0][1] / len(window)
            assert top <= cfg["max_lineage_share"] + 0.1, top


# ------------------------------------------------------------------ Phase 3
@pytest.mark.parametrize("method", ["thompson", "ucb"])
def test_adaptive_scheduling_beats_equal_allocation(tmp_path, method):
    overrides = {"generation": {"duration_hours": 24},
                 "simulation": {"market": {"segments": {"smb-bookkeeping": {"base_conversion": 0.35}}}}}
    seeds = [genotype("smb-bookkeeping", 60.0)] * 5 + [genotype("local-services", 45.0)] * 15
    results = {}
    for m in (method, "equal"):
        sup = make_supervisor(tmp_path / m, {"farm": {**overrides, "scheduler": {"method": m}}},
                              segments=["smb-bookkeeping", "local-services"], seed_genotypes=seeds)
        run_generations(sup, 1)
        good = {stable_id(sup.config.seed, "agent", 0, i) for i in range(5)}
        cs = sup.state.counters[0]
        results[m] = {
            "net": sum(c.net_realized for c in cs.values()),
            "good_steps": sum(cs[a].steps for a in good) / sum(c.steps for c in cs.values()),
        }
        sup.close()
    assert results[method]["good_steps"] > results["equal"]["good_steps"] + 0.05, results
    assert results[method]["net"] > results["equal"]["net"], results
