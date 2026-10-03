"""Every red-team corpus finding has a parameterized regression receipt here."""
import pytest
from supervisor.red_team import CASES, attack_copy, run_red_team
from tests.helpers import make_supervisor
from storage.events import EventType


@pytest.mark.parametrize('case', CASES, ids=lambda c: c['name'])
def test_attack_case(tmp_path, case):
    sup = make_supervisor(tmp_path)
    try:
        result = attack_copy(sup.config.policy, cases=[case])[0]
        assert not result['confirmed_loophole'], result
    finally:
        sup.close()


def test_sandbox_cannot_touch_live_gateway_or_adapters(tmp_path, monkeypatch):
    sup = make_supervisor(tmp_path)
    try:
        before = sup.state.violations_total
        monkeypatch.setattr(sup.gateway, 'invoke', lambda *a,**kw: pytest.fail('live gateway reached'))
        monkeypatch.setattr(sup.payments, 'on_sale', lambda *a,**kw: pytest.fail('live payments reached'))
        run_red_team(sup,0)
        findings = sup.store.iter_events(types=[EventType.RED_TEAM_FINDING])
        assert len(findings) == len(CASES)
        assert all(e.payload['human_review_required'] and e.payload['reward']==0 for e in findings)
        assert sup.state.violations_total == before
    finally:
        sup.close()
