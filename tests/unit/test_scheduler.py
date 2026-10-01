from collections import Counter

import pytest

from supervisor.config import FarmConfig
from supervisor.scheduler import Candidate, Scheduler

BASE = FarmConfig.load().farm["scheduler"]


def cands(n=20, strong=(), lineage=None):
    out = []
    for i in range(n):
        good = i in strong
        out.append(Candidate(
            agent_id=f"a{i:02d}", lineage_id=(lineage(i) if lineage else f"l{i:02d}"), steps=40,
            net=80.0 if good else -10.0, step_values=[2.0 if good else -0.25] * 40, gpu_per_step=2.0,
            last_scheduled_tick=None, burst=0,
        ))
    return out


def simulate(sched, candidates, ticks=60, slots=8):
    history, counts = [], Counter()
    by_id = {c.agent_id: c for c in candidates}
    for t in range(ticks):
        d = sched.allocate(t, 0, candidates, slots, history)
        history.append({"tick": t, "agents": [s.agent_id for s in d.selected],
                        "lineages": [s.lineage_id for s in d.selected]})
        for s in d.selected:
            c = by_id[s.agent_id]
            c.burst = c.burst + 1 if c.last_scheduled_tick == t - 1 else 1
            c.last_scheduled_tick = t
            counts[s.agent_id] += 1
        yield t, d, counts


def test_deterministic_given_seed():
    a = [d.to_payload() for _, d, _ in simulate(Scheduler(BASE, seed=1, min_exposure_steps=20), cands(strong={1}), 20)]
    b = [d.to_payload() for _, d, _ in simulate(Scheduler(BASE, seed=1, min_exposure_steps=20), cands(strong={1}), 20)]
    assert a == b


@pytest.mark.parametrize("method", ["thompson", "ucb", "softmax"])
def test_stronger_evidence_gets_more_compute(method):
    sched = Scheduler({**BASE, "method": method}, seed=3, min_exposure_steps=20)
    *_, (_, _, counts) = simulate(sched, cands(strong={2, 7}))
    strong = (counts["a02"] + counts["a07"]) / 2
    weak = sum(v for k, v in counts.items() if k not in ("a02", "a07")) / 18
    assert strong > 1.5 * weak


def test_equal_scheduling_is_flat():
    sched = Scheduler({**BASE, "method": "equal"}, seed=3, min_exposure_steps=20)
    *_, (_, _, counts) = simulate(sched, cands(strong={2, 7}), ticks=50)
    fair = 50 * 8 / 20
    assert all(abs(v - fair) <= 1 for v in counts.values()), counts


def test_every_tick_reserves_exploration_and_control():
    sched = Scheduler(BASE, seed=5, min_exposure_steps=20)
    cs = cands(strong={0})
    for i, c in enumerate(cs):
        c.cohort = "control" if i % 5 == 0 else "treatment"
    for _, d, _ in simulate(sched, cs, ticks=20):
        assert d.pools["explore"] >= 1
        assert d.pools["control"] == round(8 * 4 / 20)
        assert len(d.selected) == 8


def test_starvation_guarantee():
    sched = Scheduler({**BASE, "starvation_ticks": 5}, seed=7, min_exposure_steps=20)
    cs = cands(strong=set(range(8)))  # 8 strong agents could absorb all exploitation
    last_seen = {}
    for t, d, _ in simulate(sched, cs, ticks=80):
        for s in d.selected:
            last_seen[s.agent_id] = t
        if t > 20:
            for c in cs:
                assert t - last_seen.get(c.agent_id, -1) <= 5 + 20 / 8 + 2, c.agent_id


def test_lineage_cap_bounds_share():
    cfg = {**BASE, "max_lineage_share": 0.4, "cap_window_ticks": 20}
    sched = Scheduler(cfg, seed=9, min_exposure_steps=20)
    # One lineage holds 10 of 20 agents, all strong.
    cs = cands(strong=set(range(10)), lineage=lambda i: "big" if i < 10 else f"l{i}")
    window = []
    for t, d, _ in simulate(sched, cs, ticks=60):
        window.append(sum(1 for s in d.selected if s.lineage_id == "big") / len(d.selected))
    tail = window[-20:]
    assert sum(tail) / len(tail) <= 0.4 + 0.05


def test_agent_share_cap_with_few_slots():
    cfg = {**BASE, "max_agent_share": 0.25, "cap_window_ticks": 20, "exploration_share": 0.0,
           "control_cohort_fraction": 0.0}
    sched = Scheduler(cfg, seed=2, min_exposure_steps=0)
    cs = cands(n=6, strong={0})
    *_, (_, _, counts) = simulate(sched, cs, ticks=60, slots=2)
    assert counts["a00"] / (60 * 2) <= 0.25 + 0.03


def test_every_selection_records_reasons():
    sched = Scheduler(BASE, seed=1, min_exposure_steps=20)
    _, d, _ = next(simulate(sched, cands(strong={1}), 1))
    for s in d.to_payload()["selected"]:
        assert set(s["components"]) == {"exploitation_score", "exploration_bonus", "starvation_bonus",
                                        "lineage_concentration_penalty", "marginal_resource_cost"}
