"""DESIGN §16 items 7-10 and the §15 failure list."""

import sqlite3

import pytest

from helpers import make_supervisor, reopen, run_generations, run_ticks
from storage.events import EventType
from supervisor.audit import replay_accounting, replay_selection
from supervisor.core import Supervisor, SupervisorLocked
from supervisor.evolution import CLOSE_STEPS, SimulatedCrash

SEGS = ["freelancer-templates", "smb-bookkeeping", "local-services", "enterprise-audit"]


def summary(sup):
    s = sup.state
    return {
        "agents": {a.id: (a.status, a.lineage_id, a.parent_id, a.genotype) for a in s.agents.values()},
        "counters": {g: {a: c.to_dict() for a, c in cs.items()} for g, cs in s.counters.items()},
        "generation": s.current_generation,
        "tick": s.last_tick,
        "pending": sorted(s.pending_settlements),
        "head": sup.store.head(),
    }


# ------------------------------------------------------------------ §16.7
def test_supervisor_restart_mid_generation_loses_nothing(tmp_path):
    sup = make_supervisor(tmp_path, segments=SEGS)
    run_ticks(sup, 15)
    before = summary(sup)
    sup = reopen(sup)
    after = summary(sup)
    head_before, head_after = before.pop("head"), after.pop("head")
    assert before == after
    assert head_after[0] == head_before[0] + 1  # only the SUPERVISOR_STARTED marker was added
    assert sup.clock.tick == before["tick"] + 1
    run_generations(sup, 1)
    assert sup.state.generations[0].closed
    assert replay_accounting(sup.store)["ok"]
    assert sup.store.verify_chain() == (True, None)


def test_second_supervisor_cannot_start(tmp_path):
    sup = make_supervisor(tmp_path, segments=SEGS)
    with pytest.raises(SupervisorLocked):
        Supervisor(sup.config)
    sup.close()


# ------------------------------------------------------------------ §16.8
def _run_to_close(tmp_path, name):
    sup = make_supervisor(tmp_path / name, segments=SEGS)
    run_ticks(sup, sup.config.ticks_per_generation)
    assert sup.state.current_generation == 0
    return sup


@pytest.fixture(scope="module")
def reference(tmp_path_factory):
    sup = _run_to_close(tmp_path_factory.mktemp("ref"), "ref")
    sup.close_generation()
    ref = {
        "plan": sup.state.selections[0]["plan"],
        "offspring": sup.state.generations[0].close_steps["generate_mutations"]["offspring"],
        "population": sup.state.generations[1].cohort,
    }
    sup.close()
    return ref


@pytest.mark.parametrize("step", ["stop_admission", "freeze_ledger", "calculate_fitness", "select_elites",
                                  "generate_mutations", "write_lineage", "activate_next_population"])
def test_interrupted_generation_close_is_recoverable_and_idempotent(tmp_path, reference, step):
    sup = _run_to_close(tmp_path, step)
    with pytest.raises(SimulatedCrash):
        sup.close_generation(crash_after=step)
    done = CLOSE_STEPS.index(step)
    if done < CLOSE_STEPS.index("activate_next_population"):
        # The previous population is intact until activation commits.
        assert sup.state.current_generation == 0
        assert not any(a.status == "retired" for a in sup.state.agents.values())
    sup = reopen(sup)  # recover() resumes the close
    g0 = sup.state.generations[0]
    assert g0.closed and list(g0.close_steps) == list(CLOSE_STEPS)
    starts = [e.payload["number"] for e in sup.store.iter_events(types=[EventType.GENERATION_STARTED])]
    assert starts == [0, 1]
    assert len(sup.store.iter_events(types=[EventType.SELECTION_DECIDED])) == 1
    created = [e.agent_id for e in sup.store.iter_events(types=[EventType.AGENT_CREATED])]
    assert len(created) == len(set(created))
    # Same decisions as a close that never crashed.
    assert sup.state.selections[0]["plan"] == reference["plan"]
    assert g0.close_steps["generate_mutations"]["offspring"] == reference["offspring"]
    assert sup.state.generations[1].cohort == reference["population"]
    assert replay_selection(sup, 0)["ok"]
    # Running close again is a no-op.
    head = sup.store.head()
    sup.generations.close(0)
    assert sup.store.head() == head
    run_ticks(sup, 3)  # Generation One proceeds
    assert sup.state.counters[1]


# ------------------------------------------------------------ §16.9, §16.10
def test_accounting_and_selection_replay(tmp_path):
    sup = make_supervisor(tmp_path, segments=SEGS)
    run_generations(sup, 2)
    acct = replay_accounting(sup.store)
    assert acct["ok"], acct
    for g in (0, 1):
        assert replay_selection(sup, g) == {"ok": True, "fitness_match": True, "plan_match": True,
                                            "snapshot_seq": sup.state.selections[g]["snapshot_seq"]}


def test_same_seed_and_config_reproduce_scheduler_and_selection(tmp_path):
    runs = []
    for name in ("a", "b"):
        sup = make_supervisor(tmp_path / name, segments=SEGS)
        run_generations(sup, 2)
        runs.append({
            "alloc": [e.payload for e in sup.store.iter_events(types=[EventType.SCHEDULER_ALLOCATION])],
            "selection": [e.payload for e in sup.store.iter_events(types=[EventType.SELECTION_DECIDED])],
            "fitness": [e.payload for e in sup.store.iter_events(types=[EventType.FITNESS_EVALUATED])],
            "head": sup.store.head(),
        })
        sup.close()
    assert runs[0] == runs[1]


# ------------------------------------------------------------------ §15
def test_inference_outage_pauses_work_without_corruption(tmp_path):
    sup = make_supervisor(tmp_path, segments=SEGS)
    run_ticks(sup, 3)
    sup.backend.outage = True
    before = sup.state.counters[0].copy()
    steps_before = sum(c.steps for c in before.values())
    run_ticks(sup, 4)
    assert sum(c.steps for c in sup.state.counters[0].values()) == steps_before
    held = sup.store.iter_events(types=[EventType.SCHEDULER_ALLOCATION])[-1].payload
    assert held["reason"].startswith("inference unhealthy") and not held["selected"]
    sup.backend.outage = False
    run_ticks(sup, 2)
    assert sum(c.steps for c in sup.state.counters[0].values()) > steps_before
    kinds = [e.payload.get("ok") for e in sup.store.iter_events(types=[EventType.HEALTH_EVENT])
             if e.payload.get("kind") == "backend_health"]
    assert kinds == [False, True]
    assert replay_accounting(sup.store)["ok"]


def test_jobs_failing_mid_flight_are_recorded_and_retried(tmp_path):
    sup = make_supervisor(tmp_path, segments=SEGS)
    original = sup.backend.generate
    calls = {"n": 0}

    def flaky(request):
        calls["n"] += 1
        if calls["n"] % 3 == 0:
            raise TimeoutError("tool timeout")
        return original(request)

    sup.backend.generate = flaky
    run_ticks(sup, 6)
    failed = sup.store.iter_events(types=[EventType.INFERENCE_JOB_FAILED])
    assert failed and all(e.payload["error"].startswith("TimeoutError") for e in failed)
    assert not sup.state.jobs_inflight
    assert replay_accounting(sup.store)["ok"]


def test_malformed_output_pauses_agents_on_health_and_farm_recovers(tmp_path):
    sup = make_supervisor(tmp_path, {"farm": {"runtime": {"malformed_output_threshold": 3},
                                              "inference": {"simulated": {"malformed_rate": 10.0}}}},
                          segments=SEGS)
    run_generations(sup, 1)
    assert all(a.status_reason and a.status_reason.startswith("health") for a in sup.state.agents.values()
               if a.generation_born == 0)
    plan = sup.state.selections[0]["plan"]
    assert all(r["reason"] == "health check failed" for r in plan["retirements"])
    assert all(o["origin"] == "immigrant" for o in plan["offspring"])  # nobody sick becomes a parent
    assert len(sup.state.generations[1].cohort) == sup.config.population_size


def test_corrupted_agent_state_is_reset_not_trusted(tmp_path):
    sup = make_supervisor(tmp_path, segments=SEGS)
    run_ticks(sup, 4)
    victim = sorted(sup.state.agents)[0]
    raw = sqlite3.connect(sup.data_dir / "ledger.sqlite3")
    raw.execute("UPDATE agent_state SET state = '{not json' WHERE agent_id = ?", (victim,))
    raw.commit()
    raw.close()
    run_ticks(sup, 12)
    corrupted = [e for e in sup.store.iter_events(types=[EventType.HEALTH_EVENT])
                 if e.payload.get("kind") == "state_corrupted"]
    assert corrupted and corrupted[0].agent_id == victim
    assert sup.runtime.load_state(victim)[2] is False


def test_duplicate_and_delayed_settlements_are_idempotent(tmp_path):
    sup = make_supervisor(tmp_path, {"farm": {"simulation": {"market": {"segments": {
        "smb-bookkeeping": {"settlement_delay_hours": 1, "base_conversion": 0.6}}}}}}, segments=["smb-bookkeeping"])
    run_ticks(sup, 8)
    assert sup.state.pending_settlements or sup.state.counters[0]
    now, gen, tick = sup.clock.now_dt(), sup.state.current_generation, sup.clock.tick
    head = sup.store.head()
    sup.payments.reconcile(now, gen, tick)
    first = sup.store.head()
    sup.payments.reconcile(now, gen, tick)  # duplicate webhook delivery
    assert sup.store.head() == first
    settled = [e for e in sup.store.iter_events(after_seq=0, types=[EventType.FINANCIAL_EVENT])
               if e.payload.get("settles_reference")]
    refs = [e.payload["external_reference"] for e in settled]
    assert len(refs) == len(set(refs))
    assert head[0] <= first[0]
    assert replay_accounting(sup.store)["ok"]
