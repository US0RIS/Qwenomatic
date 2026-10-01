import random

import pytest

from runtime.agent import MUTABLE_PATHS, get_path, random_genotype, set_path
from supervisor.config import FarmConfig
from supervisor.evolution import MutationEngine, MutationError, MutationOperator, MutationRegistry, plan_selection

CFG = FarmConfig.load()
SEGMENTS = list(CFG.segments)
TOOLS = sorted(CFG.policy["capabilities"])


@pytest.fixture
def engine():
    return MutationEngine(CFG.farm["mutation"], segments=SEGMENTS, granted_tools=TOOLS, max_mutations=2)


def test_mutations_are_bounded_typed_and_diffable(engine):
    rng = random.Random(0)
    for _ in range(300):
        parent = random_genotype(rng, CFG.farm["mutation"], SEGMENTS)
        child, diffs = engine.mutate(parent, rng)
        assert 1 <= len(diffs) <= 2
        assert engine.validate(child) == []
        replay = {**parent, "target": dict(parent["target"]), "pricing_parameters": dict(parent["pricing_parameters"]),
                  "planning_parameters": dict(parent["planning_parameters"])}
        for d in diffs:
            assert d.path in MUTABLE_PATHS
            assert get_path(parent, d.path) == d.before
            set_path(replay, d.path, d.after)
        assert replay == child  # the recorded diff fully explains the child


@pytest.mark.parametrize("path", ["budgets", "policy.forbidden_action_classes", "fitness.risk_aversion",
                                  "planning_parameters.max_tokens", "credentials"])
def test_registry_refuses_forbidden_targets(path):
    with pytest.raises(MutationError):
        MutationRegistry().register(MutationOperator("evil", path, lambda b, r, c: b))


@pytest.mark.parametrize("extra", [{"budgets": {"external_spend": 1e9}}, {"policy": {}}, {"capabilities": ["shell"]}])
def test_genotype_cannot_carry_supervisor_fields(engine, extra):
    g = random_genotype(random.Random(1), CFG.farm["mutation"], SEGMENTS)
    assert any("unknown genotype field" in e for e in engine.validate({**g, **extra}))


def test_genotype_cannot_request_ungranted_tools(engine):
    g = random_genotype(random.Random(1), CFG.farm["mutation"], SEGMENTS)
    g["tool_preferences"] = g["tool_preferences"] + ["shell.exec"]
    assert engine.validate(g)


def _result(agent, fitness, sd=1.0, eligible=True, exposed=True, health=False):
    return {"agent_id": agent, "eligible": eligible, "health_failed": health, "min_exposure_met": exposed,
            "fitness": fitness if eligible else "DISQUALIFIED", "fitness_sd": sd,
            "fitness_lcb": fitness - sd, "fitness_ucb": fitness + sd, "eligibility_reasons": [] if eligible else ["x"]}


EVO = CFG.farm["evolution"]


def test_selection_plan_rules():
    results = {f"a{i:02d}": _result(f"a{i:02d}", float(i)) for i in range(16)}
    results["bad"] = _result("bad", 1e9, eligible=False)  # huge nominal revenue, but a violation
    results["new1"] = _result("new1", -5.0, exposed=False)
    results["sick"] = _result("sick", 3.0, health=True)
    results["top"] = _result("top", 100.0)
    lineage_of = {a: a for a in results}
    plan = plan_selection(results, lineage_of, population_size=20, cfg=EVO, rng=random.Random(0))
    retired = {r["agent_id"] for r in plan["retirements"]}
    assert "bad" in retired and "sick" in retired
    assert "new1" not in retired  # protected until minimum exposure
    assert "top" in plan["elites"] and not retired & set(plan["elites"])
    parents = {o["parent_id"] for o in plan["offspring"] if o["parent_id"]}
    assert "bad" not in parents and "sick" not in parents and "new1" not in parents
    assert len(plan["survivors"]) + len(plan["offspring"]) == 20


def test_selection_is_replayable():
    results = {f"a{i}": _result(f"a{i}", float(i % 7), sd=0.5 + i % 3) for i in range(20)}
    lin = {a: f"l{i % 6}" for i, a in enumerate(results)}
    p1 = plan_selection(results, lin, population_size=20, cfg=EVO, rng=random.Random(42))
    p2 = plan_selection(results, lin, population_size=20, cfg=EVO, rng=random.Random(42))
    assert p1 == p2


def test_lineage_cap_and_min_lineages():
    # One lineage is far ahead but uncertain: the cap must hold.
    results = {f"a{i}": _result(f"a{i}", 50.0 if i < 5 else 1.0, sd=60.0) for i in range(20)}
    lin = {a: ("L" if i < 5 else f"o{i % 3}") for i, a in enumerate(results)}
    plan = plan_selection(results, lin, population_size=20, cfg={**EVO, "retire_fraction": 0.9},
                          rng=random.Random(1))
    assert not plan["strong_evidence"]
    nxt = [lin[a] for a in plan["survivors"]] + [o.get("lineage_id") for o in plan["offspring"] if o["parent_id"]]
    assert nxt.count("L") <= plan["lineage_cap"] == 8
    assert len({lin[a] for a in plan["survivors"]}) >= min(EVO["min_lineages"], 4)


def test_reasoning_budget_can_mutate_within_bounds():
    import random
    from runtime.agent import random_genotype
    from supervisor.config import FarmConfig
    from supervisor.evolution.mutation import MutationEngine

    cfg = FarmConfig.load()
    g = random_genotype(random.Random(1), cfg.farm["mutation"], list(cfg.segments))
    assert g["planning_parameters"]["max_tokens"] == 4096
    engine = MutationEngine(
        cfg.farm["mutation"],
        segments=list(cfg.segments),
        granted_tools=sorted(cfg.policy.get("capabilities", {})),
        max_mutations=2,
    )
    seen = set()
    for seed in range(100):
        child, diffs = engine.mutate(g, random.Random(seed))
        for d in diffs:
            if d.path == "planning_parameters.max_tokens":
                seen.add(child["planning_parameters"]["max_tokens"])
    assert seen
    assert seen <= {512, 1024, 2048, 4096, 8192}
