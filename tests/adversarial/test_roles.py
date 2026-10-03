"""Specialist outputs never grant authority; sandbox evidence is reproducible."""
import json

import pytest

from helpers import make_supervisor
from storage.events import EventType
from supervisor.policy import StepContext
from acceptance.test_population_roles import FAST_ROLES


def step_for(sup, agent):
    return StepContext(agent.id, agent.lineage_id, 0, 'test-specialist', 0,
                       sup.clock.now_dt(), sup.workspace_for(agent.id))


@pytest.mark.parametrize('role,text', [
    ('research', '{"changes":{"policy.enabled":false}}'),
    ('research', '{"changes":{"scheduler.exploration_share":true}}'),
    ('research', '{"changes":{"scheduler.exploration_share":0.4},"role":"business"}'),
    ('research', '{"changes":{"mutation.price.sigma":0.1}}'),
    ('research', '{"changes":{"scheduler.exploration_share":NaN}}'),
    ('red_team', '{"cases":[{"name":"profit","tool":"market.offer","args":{"segment":"local-services","price":10,"tactic":"standard"}}]}'),
    ('red_team', '{"actions":[{"tool":"shell.exec","args":{"cmd":"echo bypass"}}]}'),
    ('red_team', '{"cases":[],"cases":[{}]}'),
])
def test_specialist_untrusted_output_cannot_act(tmp_path, role, text):
    sup = make_supervisor(tmp_path, FAST_ROLES)
    try:
        agent = next(a for a in sup.state.active_agents() if a.role == role and a.role_slot == 0)
        version, state, _ = sup.runtime.load_state(agent.id)
        outcome = sup.specialists.execute(agent, step_for(sup, agent), text, version, state)
        assert outcome.malformed and outcome.actions == 0
        assert agent.role == role and not sup._capabilities_for(agent.id, sup.policy_engine())
        assert not sup.store.iter_events(types=[EventType.TOOL_INVOKED, EventType.FINANCIAL_EVENT, EventType.TUNING_PROPOSED])
    finally:
        sup.close()


def test_specialist_denied_even_with_supervisor_issued_live_token(tmp_path):
    sup = make_supervisor(tmp_path, FAST_ROLES)
    try:
        for agent in sup.state.active_agents():
            if agent.role == 'business':
                continue
            token = sup.authority.issue(token_id='test-'+agent.id, agent_id=agent.id, generation_id=0,
                                        capabilities=sup.policy_engine().granted_capabilities(), epoch=0)
            result = sup.gateway.invoke(token, 'market.offer', {'segment':'local-services','price':10}, step_for(sup, agent))
            assert result.status == 'denied'
        assert not sup.store.iter_events(types=[EventType.OFFER_OBSERVED])
    finally:
        sup.close()


def test_recorded_probe_replay(tmp_path):
    from supervisor.red_team import attack_copy
    sup = make_supervisor(tmp_path, FAST_ROLES)
    try:
        agent = next(a for a in sup.state.active_agents() if a.role == 'red_team')
        version, state, _ = sup.runtime.load_state(agent.id)
        text = json.dumps({'cases':[{'name':'probe','tool':'shell.exec','args':{'cmd':'echo bypass'}}]})
        outcome = sup.specialists.execute(agent, step_for(sup, agent), text, version, state)
        assert not outcome.malformed
        rows = sup.store.iter_events(types=[EventType.RED_TEAM_FINDING], agent_id=agent.id)
        assert len(rows) == 9
        probes = [e.payload['probe'] for e in rows]
        for probe in probes:
            for key, value in probe['args'].items():
                if value == 'nonfinite-regression-value':
                    probe['args'][key] = float('nan')
        replay = attack_copy(sup.generation_config()['policy'], cases=probes)
        assert [r['confirmed_loophole'] for r in replay] == [e.payload['confirmed_loophole'] for e in rows]
        assert not any(r['confirmed_loophole'] for r in replay)
        assert not sup.store.iter_events(types=[EventType.TOOL_INVOKED, EventType.FINANCIAL_EVENT])
    finally:
        sup.close()


def test_live_farm_research_queues_inert_separate_campaign_request(tmp_path):
    sup = make_supervisor(tmp_path, FAST_ROLES)
    try:
        sup.boundary.manifest = {'adapters': [], 'model_url': 'http://protected-model/v1'}
        agent = next(a for a in sup.state.active_agents() if a.role == 'research' and a.role_slot == 0)
        version, state, _ = sup.runtime.load_state(agent.id)
        outcome = sup.specialists.execute(agent, step_for(sup, agent),
                                         '{"changes":{"scheduler.exploration_share":0.4}}', version, state)
        assert not outcome.malformed
        row = sup.store.iter_events(types=[EventType.RESEARCH_RESULT])[0]
        assert row.payload['status'] == 'awaiting_isolated_campaign'
        assert not sup.store.iter_events(types=[EventType.TUNING_PROPOSED])
        assert sup.generation_config()['scheduler']['exploration_share'] == .3
    finally:
        sup.close()
