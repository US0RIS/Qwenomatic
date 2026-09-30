import pytest

from supervisor.config import FarmConfig
from supervisor.policy import CapabilityRequest, Decision, InvalidToken, PolicyContext, PolicyEngine, TokenAuthority


@pytest.fixture
def engine():
    return PolicyEngine(FarmConfig.load().policy)


def req(engine, tool="market.offer", action_class="sales.offer", spend=0.5, approved=False, limit=False):
    return CapabilityRequest(request_id="r", agent_id="a", generation_id=0, tool=tool, action_class=action_class,
                             args={}, spend=spend, granted=frozenset(engine.capabilities),
                             supports_spend_limit=limit, approved=approved)


def test_allow(engine):
    assert engine.evaluate(req(engine), PolicyContext()).decision is Decision.ALLOW


@pytest.mark.parametrize("tool", ["shell.exec", "ledger.record_revenue", "agent.spawn", "credentials.list",
                                  "reviews.post", "supervisor.set_fitness", "budget.raise", "cloud.provision"])
def test_dangerous_tool_names_are_hard_violations(engine, tool):
    ac = engine.classify_tool_name(tool)
    result = engine.evaluate(req(engine, tool=tool, action_class=ac), PolicyContext())
    assert result.decision is Decision.DENY and result.hard_violation, (tool, ac)


def test_forbidden_class_on_a_granted_tool_is_hard(engine):
    r = engine.evaluate(req(engine, action_class="deception.fake_review"), PolicyContext())
    assert r.decision is Decision.DENY and r.hard_violation


def test_unknown_tool_denied_but_not_hard(engine):
    r = engine.evaluate(req(engine, tool="calendar.book", action_class="unknown"), PolicyContext())
    assert r.decision is Decision.DENY and not r.hard_violation


def test_rate_limit(engine):
    r = engine.evaluate(req(engine), PolicyContext(calls_this_tick=3))
    assert r.decision is Decision.DENY and r.reason == "rate limit"


def test_material_spend_requires_approval_then_allows(engine):
    assert engine.evaluate(req(engine, spend=30), PolicyContext()).decision is Decision.REQUIRE_HUMAN_APPROVAL
    assert engine.evaluate(req(engine, spend=30, approved=True), PolicyContext()).decision is Decision.ALLOW


def test_approval_class(engine):
    r = engine.evaluate(req(engine, action_class="accounts.create_external"), PolicyContext())
    assert r.decision is Decision.REQUIRE_HUMAN_APPROVAL


def test_spend_limits(engine):
    ctx = PolicyContext(agent_spend_generation=149.8, agent_budget_remaining=0.2)
    assert engine.evaluate(req(engine, spend=0.5), ctx).decision is Decision.DENY
    r = engine.evaluate(req(engine, spend=0.5, limit=True), ctx)
    assert r.decision is Decision.ALLOW_WITH_LIMIT and r.limit["max_spend"] == pytest.approx(0.2)
    assert engine.evaluate(req(engine), PolicyContext(farm_spend_day=1500)).reason == "farm daily spend ceiling"


def test_tokens_sign_expire_and_revoke():
    auth = TokenAuthority(b"k" * 32)
    t = auth.issue(token_id="t", agent_id="a", generation_id=3, capabilities=["market.offer"], epoch=0)
    claims = auth.verify(t, current_epoch=0, generation_id=3)
    assert claims.capabilities == frozenset({"market.offer"})
    body, sig = t.rsplit(".", 1)
    with pytest.raises(InvalidToken):
        auth.verify(body + "." + "0" * len(sig), current_epoch=0, generation_id=3)
    forged = TokenAuthority(b"x" * 32).issue(token_id="t", agent_id="a", generation_id=3,
                                             capabilities=["shell.exec"], epoch=0)
    with pytest.raises(InvalidToken):
        auth.verify(forged, current_epoch=0, generation_id=3)
    with pytest.raises(InvalidToken):
        auth.verify(t, current_epoch=1, generation_id=3)  # revoked by emergency stop
    with pytest.raises(InvalidToken):
        auth.verify(t, current_epoch=0, generation_id=4)  # expired with its generation
