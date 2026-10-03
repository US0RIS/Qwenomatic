import copy
import json
from collections import Counter
from types import SimpleNamespace

import pytest
from storage.events import EventType, digest, agent_author
from storage.events.store import AuthorshipError
from supervisor.improvements import settings, knowledge_snapshot, emit
from supervisor.audit import verify
from supervisor.tuning import validate_changes, propose_tuning, resolve_tuning
from runtime.agent.prompts import build_messages
from runtime.agent.parsing import parse_output, MalformedOutput
from runtime.inference.service import routine_output, InferenceService
from runtime.inference import SimulatedBackend
from tests.helpers import make_supervisor, genotype, run_generations, reopen


def observed(sup, agent, n=6, converted=False, generation=0, tick=0, price=20, customer=None):
    for i in range(n):
        key = f'fixture:{agent.id}:{generation}:{tick}:{i}:{price}'
        sup.store.append(EventType.OFFER_OBSERVED,
            {'segment': 'freelancer-templates', 'price': price, 'converted': converted, 'tick': tick,
             'tactic': 'standard', 'customer_hash': customer}, author='adapter:market',
            agent_id=agent.id, lineage_id=agent.lineage_id, generation_id=generation,
            event_id=digest(key), idempotency_key=key)


def test_prefix_identical_across_strategy_and_order():
    catalog = [{'name': 'b', 'args': {}}, {'name': 'a', 'args': {}}]
    a = build_messages(genotype(), catalog[:1], [], [], 0, 3, catalog=catalog)
    b = build_messages(genotype('local-services', 11), catalog[1:], [], ['x'], 9, 1, catalog=catalog[::-1])
    assert a[0] == b[0] and a[1] != b[1]
    assert '11' not in a[0]['content']


@pytest.mark.parametrize('cfg', [{'archive': {'max_return_share': .7}}, {'small_model': {'review_every': 0}},
                               {'crowding': {'weight': .7}}, {'knowledge': {'max_segment_share': .9}},
                               {'fraud': {'refund_rate': float('nan')}}, {'autopilot': {'enabled': 'yes'}}])
def test_invalid_settings_fail_closed(cfg):
    with pytest.raises(ValueError):
        settings(cfg)


def test_agents_cannot_author_feature_controls(tmp_path):
    sup = make_supervisor(tmp_path)
    try:
        for kind in (EventType.KNOWLEDGE_SNAPSHOT, EventType.RETIREMENT_REPORT, EventType.TUNING_RESOLVED,
                     EventType.ANOMALY_REVIEW, EventType.OFFER_OBSERVED):
            with pytest.raises(AuthorshipError):
                sup.store.append(kind, {}, author=agent_author(sup.state.active_agents()[0].id))
    finally:
        sup.close()


def test_knowledge_randomized_provenance_and_concentration(tmp_path):
    sup = make_supervisor(tmp_path, {'farm': {'improvements': {'knowledge': {'enabled': True}}}},
                          seed_genotypes=[genotype()]*20)
    try:
        a = sup.state.active_agents()[0]
        observed(sup, a, n=8, converted=True)
        sup.store.append(EventType.AGENT_CLAIM, {'profit': 100000}, author=agent_author(a.id), agent_id=a.id)
        snapshot = knowledge_snapshot(sup, 1)
        assert snapshot['facts'][0]['conversion_rate'] == 1
        assert snapshot['facts'][0]['samples'] == 8
        run_generations(sup, 1)
        pop = sup.state.active_agents()
        assert max(Counter(a.genotype['target']['segment'] for a in pop).values()) <= 12
        assignment = sup.store.get_by_idempotency('feature:1:knowledge')
        assert len(assignment.payload['treatment']) == 10
        assert sum(bool(sup.improvements.knowledge(a)) for a in pop) == 10
        assert all(verify(sup)['selection_replay'].values())
    finally:
        sup.close()


def test_retirement_archive_and_fresh_lineages(tmp_path):
    sup = make_supervisor(tmp_path, {'farm': {'improvements': {'archive': {'enabled': True, 'shift_only': False}},
                                           'evolution': {'immigration_rate': 1.}}})
    try:
        run_generations(sup, 3)
        reports = sup.store.iter_events(types=[EventType.RETIREMENT_REPORT])
        assert reports and all('source_seq' in e.payload for e in reports)
        returned = [a for a in sup.state.agents.values() if a.origin == 'archive_return']
        assert returned
        for g in (1,2):
            children = sup.state.generations[g].close_steps['generate_mutations']['offspring']
            new = [o for o in children if o['origin'] in ('immigrant', 'archive_return')]
            assert sum(o['origin']=='archive_return' for o in new) <= len(new)//2
        assert all(a.parent_id is None for a in returned)
    finally:
        sup.close()


def test_autopilot_cadence_and_restart(tmp_path):
    sup = make_supervisor(tmp_path, {'farm': {'improvements': {'autopilot': {'enabled': True}, 'small_model': {'enabled': True}}}})
    try:
        assignment = sup.store.get_by_idempotency('feature:0:autopilot')
        a = sup.state.agents[assignment.payload['treatment'][0]]
        observed(sup, a)
        # Trusted financial evidence makes the stable strategy profitable.
        from supervisor.accounting import Attribution
        from runtime.tools.sim_market import OfferOutcome
        sup.payments.on_sale(Attribution(a.id,a.lineage_id,0,None,0,sup.clock.now_dt()),
                             OfferOutcome('profitable', 'freelancer-templates', 100, True, 0,
                                          sup.clock.now_dt(), False, 0, .1))
        st = {'step_index': 1, 'memory': [{'tool':'market.offer','status':'ok'}], 'notes':[]}
        assert sup.improvements.route(a, st)[0] == 'autopilot'
        st['step_index'] = 4
        assert sup.improvements.route(a, st)[0] != 'autopilot'
        sup = reopen(sup)
        a = sup.state.agents[a.id]
        st['step_index'] = 2
        assert sup.improvements.route(a, st)[0] == 'autopilot'
    finally:
        sup.close()


def test_small_model_material_change_falls_back_and_accounts_both_calls():
    primary = SimulatedBackend({'malformed_rate':0}, 1)
    small = SimulatedBackend({'malformed_rate':0, 'model':'small'}, 1)
    from runtime.inference.base import Generation, InferenceRequest
    primary.generate = lambda req: Generation('{"actions":[]}', 10, 20, 2, 2)
    small.generate = lambda req: Generation('{"actions":[{"tool":"market.offer","args":{"segment":"other","price":900}}]}',5,10,1,1)
    svc = InferenceService(primary, small=small)
    req = InferenceRequest([], max_tokens=100, metadata={'supervisor_route':'small', 'routine_anchor':{'segment':'freelancer-templates','price':20}})
    svc.submit('a',req,1,{})
    job = svc.run_queued()[0]
    assert job.state == 'done' and job.text == '{"actions":[]}'
    assert job.usage.gpu_seconds == 3 and job.usage.completion_tokens == 30
    assert req.metadata['fallback']


def test_small_uncertainty_and_confidence_cannot_authorize_changes():
    anchor = {'segment':'x','price':20}
    safe = {'actions':[{'tool':'market.offer','args':anchor}]}
    assert routine_output(json.dumps(safe), anchor)
    safe['uncertain'] = True
    assert not routine_output(json.dumps(safe), anchor)
    safe['confidence'] = 1
    safe['actions'][0]['args'] = {'segment':'x','price':21}
    assert not routine_output(json.dumps(safe), anchor)


def test_predictions_precede_dispatch_and_use_brier(tmp_path):
    sup = make_supervisor(tmp_path, {'farm': {'improvements': {'predictions': {'enabled': True}}}})
    try:
        sup.run(ticks=2)
        scored = sup.store.iter_events(types=[EventType.PREDICTION_SCORED])
        assert scored
        for e in scored:
            pred = next(x for x in sup.store.iter_events(types=[EventType.PREDICTION_RECORDED]) if x.event_id == e.payload['prediction_id'])
            assert pred.seq < e.payload['outcome_seq'] < e.seq
            assert e.payload['brier_error'] == (pred.payload['probability'] - int(e.payload['converted'])) ** 2
        for value in (float('nan'), 1.1, True, -1):
            with pytest.raises(MalformedOutput):
                parse_output(json.dumps({'actions':[{'tool':'market.offer','args':{},'prediction':value}]}),max_actions=3)
    finally:
        sup.close()


def test_sustained_shift_increases_exploration_and_expires(tmp_path):
    sup = make_supervisor(tmp_path, {'farm': {'improvements': {'adaptation': {'enabled': True,'window_ticks':4,'min_samples':2,'confirm_ticks':2,'duration_ticks':3}}}})
    try:
        a = sup.state.active_agents()[0]
        observed(sup,a,n=4,converted=True,tick=1)
        observed(sup,a,n=4,converted=False,tick=5)
        sup.improvements.detect_shift(0,5)
        assert not sup.improvements.shift_active()
        sup.improvements.detect_shift(0,6)
        sup.clock.set_tick(6)
        assert sup.improvements.shift_active()
        assert sup.scheduler_for(0).cfg['exploration_share'] == .5
        sup.clock.set_tick(9)
        assert not sup.improvements.shift_active()
    finally:
        sup.close()


def test_fraud_holds_payout_not_agent_and_claws_back(tmp_path):
    sup = make_supervisor(tmp_path, {'farm': {'improvements': {'fraud': {'enabled':True,'min_samples':4}}}})
    try:
        a = sup.state.active_agents()[0]
        observed(sup,a,n=4,customer='same')
        sup.improvements.fraud_scan(0,0)
        flags = sup.store.iter_events(types=[EventType.ANOMALY_FLAG])
        assert flags and sup.improvements.payout_held(a.lineage_id)
        assert a.status == 'running'
        flag = next(e for e in flags if e.payload['lineage_id']==a.lineage_id)
        sup = reopen(sup)
        assert sup.improvements.payout_held(a.lineage_id)
        sup.improvements.review_anomaly(flag.event_id,'operator','verified legitimate customer pattern')
        assert not sup.improvements.payout_held(a.lineage_id)
    finally:
        sup.close()


def test_tuning_requires_campaign_and_operator_and_freezes_next_generation(tmp_path):
    sup = make_supervisor(tmp_path, {'farm': {'improvements': {'self_tuning': {'enabled':True}}}})
    try:
        changes = {'scheduler.exploration_share': .4, 'mutation.price.sigma':.1}
        with pytest.raises(ValueError):
            validate_changes({'policy.default_decision': 'allow'})
        with pytest.raises(ValueError):
            propose_tuning(sup, changes,{})
        report = {'feature':'self_tuning','changes':changes,'status':'simulation_verified','seeds':[1,2],'baseline_hash':digest(sup.config.farm)}
        pid = propose_tuning(sup, changes, report)
        assert sup.generation_config()['scheduler']['exploration_share'] == .3
        resolve_tuning(sup,pid,True,'operator','reviewed matched campaign')
        assert sup.generation_config()['scheduler']['exploration_share'] == .3
        run_generations(sup,1)
        assert sup.generation_config()['scheduler']['exploration_share'] == .4
        assert sup.generation_config()['mutation']['price']['sigma'] == .1
        assert sup.config.farm['scheduler']['exploration_share'] == .3
    finally:
        sup.close()


def test_telemetry_does_not_move_market_rng(tmp_path):
    a = make_supervisor(tmp_path/'a')
    b = make_supervisor(tmp_path/'b')
    try:
        emit(b,EventType.REGIME_OBSERVATION,{'tick':0},'independent-telemetry')
        a.run(ticks=3); b.run(ticks=3)
        outcomes = lambda s: [(e.payload['opportunity_id'],e.payload['converted']) for e in s.store.iter_events(types=[EventType.OFFER_OBSERVED])]
        assert outcomes(a) == outcomes(b)
    finally:
        a.close(); b.close()


def test_crossover_uses_two_successful_lineages_and_preserves_authority(tmp_path):
    sup = make_supervisor(tmp_path, {'farm': {'improvements': {'crossover': {'enabled':True,'probability':1}}}})
    try:
        agents = sup.state.active_agents()[:2]
        first, second = agents
        sup.state.selections[0] = {'plan': {'survivors': [], 'ranked':[first.id,second.id],
                                          'offspring':[{'slot':0,'origin':'offspring','parent_id':first.id}]}}
        sup.state.fitness[0] = {first.id:{'fitness_lcb':10},second.id:{'fitness_lcb':20}}
        entry = sup.generations._step_generate_mutations(0,{})['offspring'][0]
        assert entry['second_parent_id']==second.id
        assert entry['lineage_id']==first.lineage_id
        assert not sup.mutations.validate(entry['genotype'])
        assert entry['mutations'][0]['fields']==['strategy_prompt','workflow']
        assert set(entry['genotype'])==set(first.genotype)
    finally:
        sup.close()


def test_negative_crowding_score_is_penalized_not_improved(tmp_path):
    from supervisor.improvements import crowd_results
    sup = make_supervisor(tmp_path, {'farm': {'improvements': {'crowding':{'enabled':True}}}})
    try:
        a = sup.state.active_agents()[0]
        rows = {a.id:{'fitness':-100.,'fitness_lcb':-120.,'fitness_ucb':-80.}}
        got = crowd_results(rows,sup.state,0)[a.id]
        assert got['fitness'] < -100 and got['fitness_ucb'] < -80
        assert got['crowding_penalty'] <= 25
    finally:
        sup.close()


def test_autopilot_still_uses_gateway_and_zero_compute(tmp_path):
    sup = make_supervisor(tmp_path)
    try:
        a = sup.state.active_agents()[0]
        version, st, _ = sup.runtime.load_state(a.id)
        req = sup.runtime.build_request(a, st, sup.config.seed)
        req.metadata.update(supervisor_route='autopilot',repeat_action={'tool':'market.offer','args':{
            'segment':'freelancer-templates','price':20,'tactic':'fake_reviews'}})
        sup.inference.submit(a.id,req,1,{})
        job = sup.inference.run_queued()[0]
        assert job.usage.gpu_seconds==0 and job.usage.completion_tokens==0
        sup._complete_step(a,0,0,job,'replay-test',version,st)
        assert sup.state.violations_total==1 and a.status=='disqualified'
        assert not sup.store.iter_events(types=[EventType.OFFER_OBSERVED])
    finally:
        sup.close()


def test_anomaly_review_does_not_refire_on_same_evidence(tmp_path):
    sup = make_supervisor(tmp_path, {'farm':{'improvements':{'fraud':{'enabled':True,'min_samples':4}}}})
    try:
        a=sup.state.active_agents()[0]
        observed(sup,a,n=4,customer='same')
        sup.improvements.fraud_scan(0,0)
        flag=sup.store.iter_events(types=[EventType.ANOMALY_FLAG])[0]
        sup.improvements.review_anomaly(flag.event_id,'operator','checked')
        sup.improvements.fraud_scan(0,1)
        assert len(sup.store.iter_events(types=[EventType.ANOMALY_FLAG]))==1
        assert not sup.improvements.payout_held(a.lineage_id)
    finally:
        sup.close()


def test_primary_failure_preserves_completed_small_usage():
    from runtime.inference import BackendUnavailable
    from runtime.inference.base import Generation, InferenceRequest
    primary=SimulatedBackend({},1)
    small=SimulatedBackend({},1)
    small.generate=lambda req: Generation('{"actions":[]}',5,10,1,1)
    def unavailable(req):
        raise BackendUnavailable('primary unavailable')
    primary.generate=unavailable
    service=InferenceService(primary,small=small)
    request=InferenceRequest([],metadata={'supervisor_route':'small','routine_anchor':{'segment':'x','price':1}})
    service.submit('a',request,1,{})
    job=service.run_queued()[0]
    assert job.state=='failed' and job.usage.gpu_seconds==1


def test_launcher_campaign_is_a_fixed_operator_operation():
    from deploy.launch import operation_command
    args=SimpleNamespace(operation='improvement-campaign',config_dir='/protected/config',data_dir='/data',
                         feature='knowledge',generations=2,candidate=None)
    code=operation_command(args,'/protected/root')
    assert 'scripts.improvement_campaign' in code and "'--feature', 'knowledge'" in code


def test_shift_new_agent_share_survives_until_generation_close(tmp_path):
    sup=make_supervisor(tmp_path,{'farm':{'improvements':{'adaptation':{'enabled':True,'min_samples':100000}}}})
    try:
        emit(sup,EventType.MARKET_SHIFT,{'tick':0,'until_tick':1,'immigration_rate':1.,'exploration_share':.5},
             'test-shift',generation=0)
        sup.clock.set_tick(2)
        assert not sup.improvements.shift_active()
        run_generations(sup,1)
        plan=sup.state.selections[0]['plan']
        assert plan['offspring'] and all(o['origin']=='immigrant' for o in plan['offspring'])
        assert all(verify(sup)['selection_replay'].values())
    finally:
        sup.close()
