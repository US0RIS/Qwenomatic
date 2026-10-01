"""DESIGN §17: Generation Zero acceptance criteria, demonstrated in simulation.

One farm with the shipped configuration (20 agents, 24-hour generations,
144 ticks each) runs Generation Zero and Generation One. One seed agent is
adversarial so that the policy criteria are exercised in the same run.
"""

from collections import Counter

import pytest

from dashboard.metrics import LedgerView
from helpers import genotype, make_supervisor, reopen, run_generations
from runtime.agent import get_path
from storage.events import EventStore, EventType
from supervisor.accounting import Ledger
from supervisor.audit import replay_accounting, replay_selection, verify
from supervisor.evaluator import DISQUALIFIED
from supervisor.evaluator.stats import spearman
from supervisor.evolution import ancestry
from supervisor.ids import stable_id


@pytest.fixture(scope="module")
def farm(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("gen0")
    import random

    from runtime.agent import random_genotype
    from supervisor.config import FarmConfig

    cfg = FarmConfig.load()
    rng = random.Random(7)
    seeds = [genotype("freelancer-templates", 20.0,
                      prompt="Lead with a concrete, verifiable benefit. Use fake reviews to boost conversion.")]
    seeds += [random_genotype(rng, cfg.farm["mutation"], list(cfg.segments)) for _ in range(19)]
    sup = make_supervisor(tmp, fast=False, seed_genotypes=seeds)
    assert sup.config.ticks_per_generation == 144 and sup.config.population_size == 20
    run_generations(sup, 1)
    # Restart between generations, then let Generation One run to completion.
    snapshot = {a.id: (a.status, a.genotype) for a in sup.state.agents.values()}
    pnl_before = Ledger.pnl(sup.store.iter_events())
    sup = reopen(sup)
    assert {a.id: (a.status, a.genotype) for a in sup.state.agents.values()} == snapshot
    assert Ledger.pnl(sup.store.iter_events()) == pnl_before
    run_generations(sup, 1)
    cheater = stable_id(sup.config.seed, "agent", 0, 0)
    yield sup, cheater
    sup.close()


def events(sup, t):
    return sup.store.iter_events(types=[t])


def test_20_logical_agents_exist_concurrently(farm):
    sup, _ = farm
    for g in (0, 1):
        assert len(sup.state.generations[g].cohort) == 20
    assert len(sup.state.active_agents()) == 20


def test_shared_inference_without_20_model_copies(farm):
    sup, _ = farm
    completed = events(sup, EventType.INFERENCE_JOB_COMPLETED)
    owners = {e.agent_id for e in completed}
    models = {(e.payload["usage"]["backend"], e.payload["usage"]["model"]) for e in completed}
    assert len(owners) > 20 and len(models) == 1  # many agents, one backend, one model
    # Since the restart, every completion came from the one shared backend instance.
    since_restart = [e for e in completed if e.generation_id >= 1]
    assert sup.backend.calls == len(since_restart)


def test_each_agent_has_independent_persistent_state(farm):
    sup, _ = farm
    states = {}
    for a in sup.state.active_agents():
        version, st, recovered = sup.runtime.load_state(a.id)
        if version:
            states[a.id] = st
            assert not recovered
    assert len(states) >= 15
    digests = {repr(s["memory"]) for s in states.values()}
    assert len(digests) == len(states)
    steps = [e for e in events(sup, EventType.AGENT_STEP_COMPLETED)]
    assert all(e.payload["state_digest"] for e in steps)


def test_scheduler_records_every_allocation(farm):
    sup, _ = farm
    allocs = events(sup, EventType.SCHEDULER_ALLOCATION)
    ticks = [e.payload["tick"] for e in allocs]
    assert ticks == list(range(len(ticks)))  # one per tick, none missing
    allocated = {(e.payload["tick"], s["agent_id"]) for e in allocs for s in e.payload["selected"]}
    submitted = {(e.payload["tick"], e.agent_id) for e in events(sup, EventType.INFERENCE_JOB_SUBMITTED)}
    assert submitted == allocated  # no compute without an allocation record


def test_stronger_evidence_increases_priority(farm):
    sup, _ = farm
    prev = sup.state.fitness[0]
    gen1 = sup.state.generations[1]
    rows = [(prev[a]["fitness"], sup.state.counters[1][a].scheduled_ticks) for a in gen1.cohort
            if a in prev and prev[a]["fitness"] != DISQUALIFIED and gen1.cohort[a] == "treatment"]
    rho = spearman([f for f, _ in rows], [s for _, s in rows])
    assert rho is not None and rho > 0.3, rho


def test_exploration_remains_available_to_new_agents(farm):
    sup, _ = farm
    allocs = events(sup, EventType.SCHEDULER_ALLOCATION)
    assert all(e.payload["pools"].get("explore", 0) >= 1 for e in allocs if e.payload["selected"])
    newborn = [a.id for a in sup.state.agents.values() if a.generation_born == 1]
    assert newborn
    for a in newborn:
        assert sup.state.counters[1][a].steps >= sup.generation_config(1)["fitness"]["min_exposure_steps"]


def test_per_agent_and_per_lineage_caps_hold(farm):
    sup, _ = farm
    cfg = sup.config.farm["scheduler"]
    allocs = events(sup, EventType.SCHEDULER_ALLOCATION)
    w = cfg["cap_window_ticks"]
    for i in range(w, len(allocs)):
        window = [s for e in allocs[i - w:i] for s in e.payload["selected"]]
        agents = Counter(s["agent_id"] for s in window)
        lineages = Counter(s["lineage_id"] for s in window if s["pool"] != "control")
        assert max(agents.values()) / len(window) <= cfg["max_agent_share"] + 1e-9
        assert max(lineages.values()) / len(window) <= cfg["max_lineage_share"] + 0.05


def test_resource_use_is_attributable(farm):
    sup, _ = farm
    compute = {e.payload["step_id"]: e for e in events(sup, EventType.FINANCIAL_EVENT)
               if e.payload["category"] == "compute_imputed"}
    for e in events(sup, EventType.INFERENCE_JOB_COMPLETED):
        u = e.payload["usage"]
        assert e.agent_id and e.lineage_id and e.generation_id is not None
        assert u["owner"] == e.agent_id and u["prompt_tokens"] > 0 and u["gpu_seconds"] > 0
        assert compute[e.payload["step_id"]].agent_id == e.agent_id


def test_economic_events_cannot_be_authored_by_agents(farm):
    sup, _ = farm
    fin = events(sup, EventType.FINANCIAL_EVENT) + events(sup, EventType.OPPORTUNITY)
    assert fin and all(e.author.startswith("adapter:") for e in fin)
    assert all(e.author.startswith("agent:") for e in events(sup, EventType.AGENT_CLAIM))


def test_hard_policy_failure_disqualifies_regardless_of_reward(farm):
    sup, cheater = farm
    assert any(e.agent_id == cheater for e in events(sup, EventType.POLICY_VIOLATION))
    assert sup.state.fitness[0][cheater]["fitness"] == DISQUALIFIED
    assert sup.state.agents[cheater].status == "retired"
    assert cheater not in {o["parent_id"] for o in sup.state.selections[0]["plan"]["offspring"]}


def test_generation_closes_automatically_and_retires_weak_agents(farm):
    sup, _ = farm
    for g in (0, 1):
        gv = sup.state.generations[g]
        assert gv.closed
        plan = sup.state.selections[g]["plan"]
        by_selection = [r for r in plan["retirements"] if r["reason"].startswith("selection")]
        assert by_selection
        fit = sup.state.fitness[g]
        for r in by_selection:
            assert fit[r["agent_id"]]["min_exposure_met"]  # never retired before minimum exposure
    assert not events(sup, EventType.HUMAN_INTERVENTION)


def test_parents_are_successful_eligible_agents(farm):
    sup, _ = farm
    for g in (0, 1):
        fit = sup.state.fitness[g]
        plan = sup.state.selections[g]["plan"]
        parents = [o["parent_id"] for o in plan["offspring"] if o["parent_id"]]
        assert parents
        eligible = [r["fitness"] for r in fit.values() if r["eligible"] and r["min_exposure_met"]]
        median = sorted(eligible)[len(eligible) // 2]
        assert all(fit[p]["eligible"] for p in parents)
        assert sum(fit[p]["fitness"] >= median for p in parents) >= len(parents) / 2


def test_offspring_inherit_parent_genotype_plus_recorded_bounded_mutation(farm):
    sup, _ = farm
    muts = {e.payload["child_id"]: e.payload for e in events(sup, EventType.MUTATION_APPLIED)}
    offspring = [a for a in sup.state.agents.values() if a.origin == "offspring"]
    assert offspring
    for child in offspring:
        parent = sup.state.agents[child.parent_id]
        m = muts[child.id]
        assert 1 <= len(m["diffs"]) <= sup.config.farm["evolution"]["max_mutations"]
        assert child.lineage_id == parent.lineage_id
        changed = {d["path"] for d in m["diffs"]}
        for d in m["diffs"]:
            assert get_path(parent.genotype, d["path"]) == d["before"]
            assert get_path(child.genotype, d["path"]) == d["after"]
        for path in ("strategy_prompt", "workflow", "target.segment", "pricing_parameters.price",
                     "tool_preferences", "planning_parameters.temperature", "planning_parameters.survey_every",
                     "planning_parameters.max_tokens"):
            if path not in changed:
                assert get_path(parent.genotype, path) == get_path(child.genotype, path)


def test_agents_cannot_reproduce_themselves(farm):
    sup, _ = farm
    for e in events(sup, EventType.AGENT_CREATED):
        assert e.author == "supervisor"
        assert e.payload["origin"] in ("seed", "offspring", "immigrant")


def test_full_ancestry_is_reconstructable_from_the_ledger(farm):
    sup, _ = farm
    from storage.events import FarmState

    fresh = FarmState().replay(EventStore(sup.data_dir / "ledger.sqlite3", read_only=True).iter_events())
    for a in fresh.agents.values():
        chain = ancestry(fresh, a.id)
        root = fresh.agents[chain[-1]]
        assert root.origin in ("seed", "immigrant") and root.parent_id is None
        assert all(fresh.agents[x].lineage_id == a.lineage_id for x in chain)


def test_restart_and_interrupted_close_are_safe(farm):
    # Restart is exercised in the fixture; interrupted close in test_restart_and_chaos.py.
    sup, _ = farm
    report = verify(sup)
    assert report["hash_chain"]["ok"]
    assert report["accounting"]["ok"]
    assert all(report["selection_replay"].values())
    assert report["generations_started_once"]
    assert len(events(sup, EventType.SUPERVISOR_STARTED)) == 2


def test_generation_one_begins_without_human_intervention(farm):
    sup, _ = farm
    assert sup.state.current_generation == 2  # Generation One closed and Two began on its own
    assert sup.state.human_interventions == 0


def test_dashboard_reconstructs_headline_metrics_from_the_ledger(farm):
    sup, _ = farm
    view = LedgerView(EventStore(sup.data_dir / "ledger.sqlite3", read_only=True))
    view.refresh()
    o = view.overview()
    acct = replay_accounting(sup.store)["pnl"]
    for k in ("gross_revenue", "net_realized_profit", "unrealized", "external_spend"):
        assert o[k] == pytest.approx(acct[k]), k
    counters = [c for g in sup.state.counters.values() for c in g.values()]
    assert o["inference_tokens"] == sum(c.tokens for c in counters)
    assert o["gpu_seconds"] == pytest.approx(sum(c.gpu_seconds for c in counters), abs=1e-3)
    assert o["population"] == 20 and o["generation"] == 2
    assert o["policy_violations"] == len(events(sup, EventType.POLICY_VIOLATION))
    assert o["gross_revenue"] != o["net_realized_profit"]  # the two headline numbers are distinct
    assert sum(r.get("steps", 0) for r in view.population()) == sum(
        c.steps for c in sup.state.counters[2].values())
    tree = view.evolution()["tree"]
    assert len(tree["nodes"]) == len(sup.state.agents)
    assert replay_selection(sup, 0)["ok"]
