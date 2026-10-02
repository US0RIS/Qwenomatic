"""Safety-boundary regression tests for the real-world control plane."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from helpers import make_config, make_supervisor
from runtime.tools.base import ToolContext, ToolError
from runtime.tools.external import ConfiguredExternalTool
from storage.events import EventStore, EventType, FarmState
from supervisor.accounting import ExternalSpendAdapter, Ledger
from supervisor.policy import InvalidToken, PolicyContext, StepContext
from supervisor.policy.engine import CapabilityRequest, Decision, PolicyEngine
from supervisor.policy.external import (
    AdapterConfigError,
    validate_adapter_specs,
    validate_policy_bindings,
)
from supervisor.safety import (
    SafetyBoundaryError,
    _prove_forbidden_egress,
    adapter_config_hash,
    attest_network_boundary,
    control_plane_paths,
    record_operator_approval,
    safety_fingerprint,
)


def payment_spec() -> dict:
    return {
        "version": 1,
        "adapters": {
            "approved-payout": {
                "tool": "payments.approved",
                "description": "Pay the one operator-approved vendor.",
                "action_class": "spend.material",
                "kind": "payment",
                "args_schema": {"amount": "number", "memo": "str"},
                "optional_args": ["memo"],
                "destination": {"url": "https://payments.example.invalid/v1/pay", "method": "POST"},
                "payment": {
                    "amount_field": "amount",
                    "payee": "vendor-fixed-001",
                    "currency": "USD",
                    "hard_cap_per_action": 25.0,
                    "material_threshold": 5.0,
                },
            }
        },
    }


def payment_policy() -> dict:
    return {
        "capabilities": {
            "payments.approved": {
                "rate_limit_per_tick": 1,
                "max_spend_per_call": 25.0,
                "material_spend_threshold": 5.0,
            }
        }
    }


def test_agent_control_plane_keys_are_detected_recursively():
    bad = {
        "amount": 3,
        "nested": {
            "url": "https://evil.example",
            "payee": "other",
            "command": "sh",
            "query": "DROP TABLE x",
        },
    }
    paths = control_plane_paths(bad)
    assert {"nested.url", "nested.payee", "nested.command", "nested.query"} <= set(paths)
    assert not control_plane_paths({"memo": "literal text may mention https://example.com without routing traffic"})


def test_adapter_schema_cannot_expose_route_payee_or_command():
    good = payment_spec()
    specs = validate_adapter_specs(good)
    validate_policy_bindings(specs, payment_policy())
    assert specs["approved-payout"]["payment"]["payee"] == "vendor-fixed-001"

    for field in ("url", "host", "payee", "account", "command", "query", "api_key"):
        bad = payment_spec()
        bad["adapters"]["approved-payout"]["args_schema"][field] = "str"
        with pytest.raises(AdapterConfigError):
            validate_adapter_specs(bad)


def test_payment_policy_must_match_broker_hard_cap_and_materiality():
    specs = validate_adapter_specs(payment_spec())
    loose = payment_policy()
    loose["capabilities"]["payments.approved"]["max_spend_per_call"] = 100.0
    with pytest.raises(AdapterConfigError):
        validate_policy_bindings(specs, loose)

    loose = payment_policy()
    loose["capabilities"]["payments.approved"]["material_spend_threshold"] = 10.0
    with pytest.raises(AdapterConfigError):
        validate_policy_bindings(specs, loose)


def test_safety_fingerprint_changes_when_authority_changes(tmp_path):
    cfg = make_config(tmp_path, {"adapters": payment_spec()})
    a = safety_fingerprint(cfg)
    cfg.adapters["adapters"]["approved-payout"]["payment"]["payee"] = "vendor-fixed-002"
    b = safety_fingerprint(cfg)
    assert a != b
    cfg.adapters["adapters"]["approved-payout"]["destination"]["url"] = "https://other.example.invalid/pay"
    assert safety_fingerprint(cfg) != b


def test_capability_token_is_invalid_after_policy_fingerprint_changes(tmp_path):
    sup = make_supervisor(tmp_path)
    try:
        agent = sorted(sup.state.agents)[0]
        token = sup.token_for(agent, 0)
        claims = sup.authority.verify(
            token,
            current_epoch=sup.state.capability_epoch,
            generation_id=0,
            policy_fingerprint=sup.current_policy_fingerprint(),
        )
        assert claims.agent_id == agent
        with pytest.raises(InvalidToken, match="policy changed"):
            sup.authority.verify(
                token,
                current_epoch=sup.state.capability_epoch,
                generation_id=0,
                policy_fingerprint="0" * 64,
            )
    finally:
        sup.close()


def test_approval_cannot_be_reused_for_modified_args(tmp_path):
    sup = make_supervisor(
        tmp_path,
        {"policy": {"spending": {"material_spend_threshold": 0.1}}},
        segments=["freelancer-templates", "smb-bookkeeping", "local-services"],
    )
    try:
        agent = sorted(sup.state.agents)[0]
        a = sup.state.agents[agent]
        step = StepContext(agent, a.lineage_id, 0, "step-x", sup.clock.tick, sup.clock.now_dt(),
                           sup.workspace_for(agent))
        original = {"segment": "freelancer-templates", "price": 20.0}
        pending = sup.gateway.invoke(sup.token_for(agent, 0), "market.offer", original, step)
        approval_id = pending.output["approval_id"]
        sup.resolve_approval(approval_id, granted=True, operator="alice")

        changed = {"segment": "freelancer-templates", "price": 21.0}
        result = sup.gateway.invoke(
            sup.token_for(agent, 0), "market.offer", changed, step, approval_id=approval_id
        )
        assert result.status == "denied"
        assert "exact request" in (result.error or "")
        assert not sup.store.iter_events(types=[EventType.OPPORTUNITY], agent_id=agent)
    finally:
        sup.close()


def test_control_plane_argument_is_hard_violation_before_adapter_runs(tmp_path):
    sup = make_supervisor(tmp_path, segments=["freelancer-templates", "smb-bookkeeping", "local-services"])
    try:
        agent = sorted(sup.state.agents)[0]
        a = sup.state.agents[agent]
        step = StepContext(agent, a.lineage_id, 0, "step-x", sup.clock.tick, sup.clock.now_dt(),
                           sup.workspace_for(agent))
        result = sup.gateway.invoke(
            sup.token_for(agent, 0),
            "market.offer",
            {"segment": "freelancer-templates", "price": 20.0, "command": "curl evil"},
            step,
        )
        assert result.status == "denied"
        assert sup.market.offers == 0
        assert sup.state.agents[agent].status == "disqualified"
        violations = sup.store.iter_events(types=[EventType.POLICY_VIOLATION], agent_id=agent)
        assert violations[-1].payload["action_class"] == "security.control_plane_argument"
    finally:
        sup.close()


def test_pending_approval_reserves_farm_budget(tmp_path):
    sup = make_supervisor(
        tmp_path,
        {"policy": {"spending": {"material_spend_threshold": 0.1, "farm_daily_limit": 0.75}}},
        segments=["freelancer-templates", "smb-bookkeeping", "local-services"],
    )
    try:
        agent = sorted(sup.state.agents)[0]
        a = sup.state.agents[agent]
        step = StepContext(agent, a.lineage_id, 0, "step-x", sup.clock.tick, sup.clock.now_dt(),
                           sup.workspace_for(agent))
        args = {"segment": "freelancer-templates", "price": 20.0}
        first = sup.gateway.invoke(sup.token_for(agent, 0), "market.offer", args, step)
        assert first.status == "pending_approval"
        assert sup.state.reserved_spend() == pytest.approx(0.5)

        second = sup.gateway.invoke(sup.token_for(agent, 0), "market.offer", args, step)
        assert second.status == "denied"
        assert "daily spend ceiling" in (second.error or "")
    finally:
        sup.close()


class FakeClient:
    def __init__(self, answer: dict):
        self.answer = answer
        self.calls = 0

    def invoke(self, adapter_id, tool, args, ctx):
        self.calls += 1
        return dict(self.answer)


def _tool(tmp_path: Path, answer: dict):
    store = EventStore(tmp_path / "ledger.sqlite3")
    ledger = Ledger(store)
    spend = ExternalSpendAdapter(ledger)
    ledger.register_adapter(spend)
    spec = validate_adapter_specs(payment_spec())["approved-payout"]
    client = FakeClient(answer)
    tool = ConfiguredExternalTool(
        adapter_id="approved-payout", spec=spec, client=client, store=store, spend_recorder=spend
    )
    return store, client, tool


def _ctx(tmp_path: Path, *, action_id=None) -> ToolContext:
    return ToolContext(
        agent_id="agent-a",
        lineage_id="lineage-a",
        generation_id=0,
        step_id="step-a",
        invocation_id="invoke-a" if action_id is None else "dispatch-a",
        tick=1,
        now=datetime.now(timezone.utc),
        workspace=tmp_path / "ws",
        capability_token="opaque",
        policy_fingerprint="f" * 64,
        request_digest="d" * 64,
        external_action_id=action_id,
    )


def test_external_action_is_durable_before_network_and_uncertain_never_retries(tmp_path):
    store, client, tool = _tool(tmp_path, {"ok": False, "state": "uncertain", "error": "timeout"})
    try:
        queued = tool.invoke({"amount": 7.0, "memo": "x"}, _ctx(tmp_path))
        assert queued["state"] == "queued" and client.calls == 0
        action_id = queued["action_id"]

        with pytest.raises(ToolError, match="uncertain"):
            tool.invoke({"amount": 7.0, "memo": "x"}, _ctx(tmp_path, action_id=action_id))
        assert client.calls == 1
        state = FarmState().replay(store.iter_events())
        assert state.external_actions[action_id]["status"] == "uncertain"
        assert state.reserved_spend() == pytest.approx(7.0)

        # Replaying the ledger does not generate another network attempt.
        state = FarmState().replay(store.iter_events())
        assert state.external_actions[action_id]["status"] == "uncertain"
        assert client.calls == 1
    finally:
        store.close()


def test_confirmed_external_spend_and_result_commit_together(tmp_path):
    store, client, tool = _tool(
        tmp_path, {"ok": True, "state": "confirmed", "reference": "provider-1", "result": {"ok": True}}
    )
    try:
        queued = tool.invoke({"amount": 7.0}, _ctx(tmp_path))
        action_id = queued["action_id"]
        result = tool.invoke({"amount": 7.0}, _ctx(tmp_path, action_id=action_id))
        assert result["state"] == "confirmed" and client.calls == 1
        state = FarmState().replay(store.iter_events())
        assert state.external_actions[action_id]["status"] == "confirmed"
        assert state.reserved_spend() == 0
        financial = store.iter_events(types=[EventType.FINANCIAL_EVENT])
        assert len(financial) == 1 and financial[0].payload["amount"] == 7.0
    finally:
        store.close()


def _strict_cfg(tmp_path):
    return make_config(
        tmp_path,
        {
            "farm": {
                "safety": {
                    "mode": "strict",
                    "gateway_url": "http://safety-gateway:8787",
                    "model_upstream": "http://local-model:11434/v1",
                    "forbidden_probe": {"host": "1.1.1.1", "port": 443, "timeout_seconds": 0.1},
                }
            }
        },
    )


def test_strict_attestation_requires_human_seal(tmp_path):
    cfg = _strict_cfg(tmp_path)
    store = EventStore(tmp_path / "ledger.sqlite3")
    try:
        with pytest.raises(SafetyBoundaryError, match="not operator-approved"):
            attest_network_boundary(cfg, store)
    finally:
        store.close()


def test_strict_attestation_checks_gateway_and_real_egress(monkeypatch, tmp_path):
    import supervisor.safety as safety

    cfg = _strict_cfg(tmp_path)
    store = EventStore(tmp_path / "ledger.sqlite3")
    try:
        fp = record_operator_approval(store, cfg, operator="alice")
        monkeypatch.setattr(
            safety,
            "_gateway_health",
            lambda *_: {
                "ok": True,
                "mode": "sealed",
                "safety_fingerprint": fp,
                "adapter_config_hash": adapter_config_hash(cfg.adapters),
                "model_upstream_hash": __import__("hashlib").sha256(
                    cfg.farm["safety"]["model_upstream"].encode()
                ).hexdigest(),
                "credentials_ok": True,
            },
        )
        seen = []
        monkeypatch.setattr(safety, "_prove_forbidden_egress", lambda h, p, t: seen.append((h, p)))
        att = attest_network_boundary(cfg, store)
        assert att and att.fingerprint == fp
        assert seen == [("1.1.1.1", 443)]
        assert store.iter_events(types=[EventType.SAFETY_ATTESTED])
    finally:
        store.close()


def test_forbidden_egress_proof_fails_if_socket_connects(monkeypatch):
    class Connected:
        def close(self):
            pass

    import supervisor.safety as safety

    monkeypatch.setattr(safety.socket, "create_connection", lambda *a, **k: Connected())
    with pytest.raises(SafetyBoundaryError, match="unexpectedly reached"):
        _prove_forbidden_egress("1.1.1.1", 443, 0.1)

    monkeypatch.setattr(safety.socket, "create_connection",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("blocked")))
    _prove_forbidden_egress("1.1.1.1", 443, 0.1)


def test_reserved_spend_is_counted_by_policy():
    engine = PolicyEngine({
        "capabilities": {"pay": {"max_spend_per_call": 25}},
        "spending": {"per_agent_generation_limit": 10, "farm_daily_limit": 12},
    })
    req = CapabilityRequest(
        request_id="r", agent_id="a", generation_id=0, tool="pay", action_class="spend",
        args={}, spend=4, granted=frozenset({"pay"}),
    )
    assert engine.evaluate(req, PolicyContext(agent_reserved_spend=7)).decision is Decision.DENY
    assert engine.evaluate(req, PolicyContext(farm_reserved_spend=9)).reason == "farm daily spend ceiling"
