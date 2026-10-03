"""17/2/1 roles exercise real scheduling, inference, campaigns and sandbox probes.

Like the existing simulation suite, this uses the test-only empty boundary;
it does not claim kernel isolation or real-Qwen performance verification.
"""
from collections import Counter
import copy
import json

import pytest

from dashboard.metrics import LedgerView
from helpers import make_supervisor, reopen, run_generations
from storage.events import EventType
from supervisor.audit import verify
from supervisor.evolution.generation import SimulatedCrash
from supervisor.roles import layout
from supervisor.tuning import resolve_tuning

FAST_ROLES = {'farm': {'roles': {'enabled': True, 'every_ticks': 2},
                      'generation': {'duration_hours': 4 / 3600, 'tick_seconds': 1},
                      'inference': {'simulated': {'malformed_rate': 0}}}}


def counts(sup):
    return Counter(a.role for a in sup.state.active_agents())


@pytest.mark.parametrize('seed', [101, 202])
def test_split_runs_all_roles_and_replays(tmp_path, seed):
    overrides = copy.deepcopy(FAST_ROLES)
    overrides['farm']['farm'] = {'seed': seed}
    sup = make_supervisor(tmp_path, overrides)
    try:
        run_generations(sup, 2)
        assert counts(sup) == {'business': 17, 'research': 2, 'red_team': 1}
        for g in (0, 1, 2):
            assert Counter(sup.state.agents[a].role for a in sup.state.generations[g].cohort) == counts(sup)
        results = sup.store.iter_events(types=[EventType.RESEARCH_RESULT])
        assert len(results) == 4
        assert all(e.payload['status'] == 'simulation_verified' for e in results)
        assert all(e.payload['campaign']['population_scope'].startswith('business-only') for e in results)
        assert all(e.payload['campaign']['business_population_size'] == 17 for e in results)
        assert all((sup.data_dir / e.payload['report_path']).is_file() for e in results)
        proposals = sup.store.iter_events(types=[EventType.TUNING_PROPOSED])
        assert proposals and not sup.store.iter_events(types=[EventType.TUNING_RESOLVED])
        assert sup.generation_config()['scheduler']['exploration_share'] == .3
        red = sup.store.iter_events(types=[EventType.RED_TEAM_FINDING])
        assert red and any(e.payload['generated'] for e in red)
        assert all(not e.payload['confirmed_loophole'] and e.payload['reward'] == 0 for e in red)
        for a in sup.state.active_agents():
            if a.role != 'business':
                assert not sup._capabilities_for(a.id, sup.policy_engine())
                assert a.budgets['external_spend'] == 0 and a.budgets['tool_calls'] == 0
                assert sup.state.counter(0, a.id).gpu_seconds > 0
                assert sup.state.counter(0, a.id).gross_revenue == 0
                assert sup.state.fitness[0][a.id]['metric']
        business_ids = {a.id for a in sup.state.agents.values() if a.role == 'business'}
        for e in sup.store.iter_events(types=[EventType.SELECTION_DECIDED]):
            assert set(e.payload['plan']['ranked']) <= business_ids
            assert set(e.payload['plan']['parent_weights']) <= business_ids
        allocations = sup.store.iter_events(types=[EventType.SCHEDULER_ALLOCATION])
        assert all(len(e.payload['selected']) <= e.payload['slots'] for e in allocations)
        assert {'research', 'red_team'} <= {s['pool'] for e in allocations for s in e.payload['selected']}
        audit = verify(sup)
        assert audit['hash_chain']['ok'] and audit['accounting']['ok']
        assert all(audit['selection_replay'].values()) and audit['generations_started_once']
        view = LedgerView(sup.store)
        view.refresh()
        assert view.overview()['role_counts'] == counts(sup)
        assert {a['role'] for a in view.population()} == {'business', 'research', 'red_team'}
        snapshot = {a.id: (a.role, a.role_slot, a.genotype) for a in sup.state.active_agents()}
        sup = reopen(sup)
        assert {a.id: (a.role, a.role_slot, a.genotype) for a in sup.state.active_agents()} == snapshot
    finally:
        sup.close()


def test_migration_occurs_only_at_boundary_and_survives_close_crash(tmp_path):
    legacy = copy.deepcopy(FAST_ROLES)
    legacy['farm']['roles']['enabled'] = False
    sup = make_supervisor(tmp_path, legacy)
    try:
        ids = {a.id for a in sup.state.active_agents()}
        sup.config.farm['roles']['enabled'] = True
        assert counts(sup) == {'business': 20}
        assert not sup.generation_config()['roles']['enabled']
        with pytest.raises(SimulatedCrash):
            sup.close_generation(crash_after='select_elites')
        sup = reopen(sup)
        assert counts(sup) == {'business': 17, 'research': 2, 'red_team': 1}
        assert len(ids & {a.id for a in sup.state.active_agents()}) == 17
        assert all(verify(sup)['selection_replay'].values())
    finally:
        sup.close()


def test_specialist_failure_replaced_without_business_parent(tmp_path):
    sup = make_supervisor(tmp_path, FAST_ROLES)
    try:
        old = next(a for a in sup.state.active_agents() if a.role == 'research')
        sup.store.append(EventType.AGENT_STATUS_CHANGED, {'status': 'paused', 'reason': 'health failure'},
                         agent_id=old.id, lineage_id=old.lineage_id, generation_id=0)
        # Paused alone is not a health failure in the evaluator; record failed jobs.
        sup.store.append(EventType.INFERENCE_JOB_FAILED, {'job_id': 'failed', 'tick': 0},
                         agent_id=old.id, lineage_id=old.lineage_id, generation_id=0)
        sup.close_generation()
        assert sup.state.agents[old.id].status == 'retired'
        replacement = next(a for a in sup.state.active_agents() if a.role == old.role and a.role_slot == old.role_slot)
        assert replacement.id != old.id and replacement.parent_id is None
        assert counts(sup) == {'business': 17, 'research': 2, 'red_team': 1}
    finally:
        sup.close()


def test_specialists_wait_under_one_slot_and_obey_stop_and_outage(tmp_path):
    overrides = copy.deepcopy(FAST_ROLES)
    overrides['farm']['inference']['slots_per_tick'] = 1
    overrides['farm']['scheduler'] = {'max_agent_share': 1, 'max_lineage_share': 1, 'max_burst_ticks': 100}
    sup = make_supervisor(tmp_path, overrides)
    try:
        for _ in range(3):
            sup.tick()
        owners = {e.agent_id for e in sup.store.iter_events(types=[EventType.INFERENCE_JOB_COMPLETED])}
        assert {a.id for a in sup.state.active_agents() if a.role != 'business'} <= owners
        sup.backend.outage = True
        count = len(sup.store.iter_events(types=[EventType.INFERENCE_JOB_SUBMITTED]))
        sup.tick()
        assert len(sup.store.iter_events(types=[EventType.INFERENCE_JOB_SUBMITTED])) == count
        sup.emergency_stop('test stop', operator='test')
        sup.backend.outage = False
        sup.tick()
        assert len(sup.store.iter_events(types=[EventType.INFERENCE_JOB_SUBMITTED])) == count
    finally:
        sup.close()


def test_research_approval_applies_only_next_generation(tmp_path):
    sup = make_supervisor(tmp_path, FAST_ROLES)
    try:
        sup.tick()
        proposal = next(e for e in sup.store.iter_events(types=[EventType.TUNING_PROPOSED])
                        if 'scheduler.exploration_share' in e.payload['changes'])
        resolve_tuning(sup, proposal.payload['proposal_id'], True, 'operator', 'Reviewed isolated campaign')
        assert sup.generation_config()['scheduler']['exploration_share'] == .3
        sup.close_generation()
        assert sup.generation_config()['scheduler']['exploration_share'] == .4
    finally:
        sup.close()


def test_default_configuration_and_invalid_quota(tmp_path):
    from supervisor.config import FarmConfig
    assert FarmConfig.load().farm['roles']['enabled']
    assert layout(FarmConfig.load().farm)['counts'] == {'business': 17, 'research': 2, 'red_team': 1}
    with pytest.raises(ValueError):
        FarmConfig.load(overrides={'farm': {'farm': {'population_size': 3}}})
    with pytest.raises(ValueError):
        FarmConfig.load(overrides={'farm': {'roles': {'research': True}}})


def test_old_closed_generations_replay_and_old_interrupted_close_finishes(tmp_path, monkeypatch):
    from supervisor.evolution.generation import GenerationManager
    legacy = copy.deepcopy(FAST_ROLES)
    legacy['farm']['roles']['enabled'] = False
    sup = make_supervisor(tmp_path, legacy, bootstrap=False)
    append = sup.store.append
    def old_append(kind, payload, **kwargs):
        if kind == EventType.GENERATION_STARTED:
            payload = copy.deepcopy(payload)
            payload['config'].pop('roles', None)
            payload['config']['hashes'].pop('roles', None)
        return append(kind, payload, **kwargs)
    monkeypatch.setattr(sup.store, 'append', old_append)
    monkeypatch.setattr(GenerationManager, '_step_freeze_ledger',
                        lambda self, g, done: dict(zip(('snapshot_seq', 'snapshot_hash'), self.sup.store.head())))
    try:
        sup.bootstrap()
        with pytest.raises(SimulatedCrash):
            sup.close_generation(crash_after='select_elites')
        assert 'role_layout' not in sup.state.selections[0]['plan']
        monkeypatch.undo()
        sup.config.farm['roles']['enabled'] = True
        sup = reopen(sup)
        # Finish the already-frozen legacy close first; migrate at the next boundary.
        assert counts(sup) == {'business': 20}
        assert verify(sup)['selection_replay'][0]
        sup.close_generation()
        assert counts(sup) == {'business': 17, 'research': 2, 'red_team': 1}
        assert all(verify(sup)['selection_replay'].values())
    finally:
        sup.close()


def test_failed_campaign_consumes_attempt_budget_and_keeps_inference_charge(tmp_path, monkeypatch):
    import supervisor.experiments.improvements
    monkeypatch.setattr(supervisor.experiments.improvements, 'run_campaign',
                        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError('campaign failed verification')))
    sup = make_supervisor(tmp_path, FAST_ROLES)
    try:
        sup.tick()
        research = [a for a in sup.state.active_agents() if a.role == 'research']
        assert len(sup.store.iter_events(types=[EventType.RESEARCH_STARTED])) == 2
        for a in research:
            assert sup.state.counter(0, a.id).gpu_seconds > 0
            assert sup.state.counter(0, a.id).malformed == 1
        sup.tick()
        sup.tick()
        assert len(sup.store.iter_events(types=[EventType.RESEARCH_STARTED])) == 2
        assert not sup.store.iter_events(types=[EventType.TUNING_PROPOSED])
        assert verify(sup)['accounting']['ok']
    finally:
        sup.close()
