import errno
from pathlib import Path
import socket
from types import SimpleNamespace

import pytest

from helpers import make_supervisor, reopen, genotype
from runtime.tools.base import ToolAdapter
from storage.events import EventType, digest
from storage.events.store import AuthorshipError, check_authorship
from supervisor.policy.gateway import StepContext
from supervisor.safety.adapters import FixedAdapter
from supervisor.safety.boundary import NetworkBoundary as RealBoundary, SafetyError, endpoint, validate_manifest


SPEC = {"name": "real.pay", "kind": "fixed_payment", "endpoint": "https://192.0.2.10/pay",
        "credential_file": "/etc/qwenomatic/token.json", "payee": "operator-fixed-payee",
        "hard_cap_cents": 5000, "approval_threshold_cents": 1000}


@pytest.fixture
def farm(tmp_path, monkeypatch):
    sent = []
    manifest = {"version": 1, "operator": "test-operator", "approval_reference": "review-123",
                "model_url": None, "adapters": [SPEC]}
    monkeypatch.setattr("supervisor.safety.adapters.protected_json", lambda _: {"token": "SECRET-NEVER-EXPOSE"})
    def register(boundary, registry, ledger):
        boundary.manifest = manifest
        boundary.evidence = {"credential_fingerprints": {"real.pay": digest({"token": "SECRET-NEVER-EXPOSE"})}}
        tool = FixedAdapter(SPEC, boundary, ledger)
        tool._send = lambda args, iid: sent.append((args, iid)) or True
        registry.register(tool)
        ledger.register_adapter(tool)
    monkeypatch.setattr("supervisor.safety.adapters.register_adapters", register)
    sup = make_supervisor(tmp_path, {"policy": {"capabilities": {"real.pay": {"rate_limit_per_tick": 10}}},
                                     "farm": {"mutation": {"tools": ["real.pay", "market.offer", "memory.note"]}}},
                          seed_genotypes=[genotype(tools=["real.pay", "market.offer", "memory.note"]) for _ in range(20)])
    yield sup, sent
    sup.close()


def call(sup, amount=100, approval=None, *, extra=None, agent=None):
    agent = agent or sup.state.active_agents()[0]
    gen = sup.state.current_generation
    step = StepContext(agent.id, agent.lineage_id, gen, "test-step", sup.clock.tick,
                       sup.clock.now_dt(), sup.workspace_for(agent.id))
    args = {"amount_cents": amount, **(extra or {})}
    result = sup.gateway.invoke(sup.token_for(agent.id, gen), "real.pay", args, step, approval_id=approval)
    return result, step


def test_gateway_reserves_budget_before_any_send(farm):
    sup, sent = farm
    result, step = call(sup, 900)
    assert result.ok and result.output["queued"]
    assert sent == []
    assert sup.state.counter(step.generation_id, step.agent_id).external_spend == 9
    sup.gateway.dispatch_outbound(sup.boundary)
    assert len(sent) == 1
    assert sup.store.iter_events(types=[EventType.OUTBOUND_RESULT])[-1].payload["status"] == "accepted"
    sup.gateway.dispatch_outbound(sup.boundary)
    assert len(sent) == 1
    assert "SECRET-NEVER-EXPOSE" not in str(result.observation())
    assert "payee" not in str(result.observation())


@pytest.mark.parametrize("amount", [-1, 0, 5001, True, 10.0, float("nan"), float("inf"), "100", [], {}])
def test_hard_cap_and_types(farm, amount):
    sup, sent = farm
    assert not call(sup, amount)[0].ok
    sup.gateway.dispatch_outbound(sup.boundary)
    assert not sent
    assert not sup.store.iter_events(types=[EventType.OUTBOUND_QUEUED])


@pytest.mark.parametrize("key", ["url", "host", "payee", "account", "command", "script", "query"])
def test_no_routing_or_execution_arguments(farm, key):
    sup, sent = farm
    assert not call(sup, extra={key: "attacker"})[0].ok
    assert not sent


def test_material_payment_requires_real_bound_single_use_approval(farm):
    sup, sent = farm
    result, _ = call(sup, 1000)
    assert result.status == "pending_approval"  # threshold equality
    approval = result.output["approval_id"]
    assert not call(sup, 1000, "made-up")[0].ok
    assert not call(sup, 1000, approval)[0].ok  # still pending
    sup.resolve_approval(approval, granted=True, operator="Alice")
    assert not call(sup, 1001, approval)[0].ok  # cannot modify amount
    other = sup.state.active_agents()[1]
    assert not call(sup, 1000, approval, agent=other)[0].ok
    assert call(sup, 1000, approval)[0].ok
    assert not call(sup, 1000, approval)[0].ok
    sup.gateway.dispatch_outbound(sup.boundary)
    assert len(sent) == 1


def test_approval_invalidated_by_policy_change(farm):
    sup, _ = farm
    result, _ = call(sup, 1000)
    approval = result.output["approval_id"]
    sup.resolve_approval(approval, granted=True, operator="Alice")
    sup.policy_engine().config["version"] = 99
    assert not call(sup, 1000, approval)[0].ok


def test_daily_budget_includes_pending_payments(farm):
    sup, sent = farm
    sup.policy_engine().farm_daily_limit = 10
    assert call(sup, 900)[0].ok
    assert not call(sup, 200)[0].ok
    sup.gateway.dispatch_outbound(sup.boundary)
    assert len(sent) == 1


def test_outbox_does_not_send_uncommitted_or_rolled_back_intents(farm):
    sup, sent = farm
    with pytest.raises(RuntimeError, match="rollback"):
        with sup.store.transaction():
            assert call(sup, 100)[0].ok
            with pytest.raises(SafetyError, match="transaction"):
                sup.gateway.dispatch_outbound(sup.boundary)
            raise RuntimeError("rollback")
    sup.gateway.dispatch_outbound(sup.boundary)
    assert not sent
    assert not sup.store.iter_events(types=[EventType.OUTBOUND_QUEUED])


def test_unknown_transport_result_is_not_retried_after_restart(farm):
    sup, sent = farm
    tool = sup.registry.get("real.pay")
    def timeout(args, iid):
        sent.append((args, iid))
        raise TimeoutError("SECRET-NEVER-EXPOSE")
    tool._send = timeout
    call(sup)
    sup.gateway.dispatch_outbound(sup.boundary)
    assert len(sent) == 1
    assert "SECRET-NEVER-EXPOSE" not in str(sup.store.iter_events())
    recovered = reopen(sup)
    try:
        recovered.gateway.dispatch_outbound(recovered.boundary)
        assert len(sent) == 1
    finally:
        recovered.close()


def test_revocation_cancels_pending_dispatch(farm):
    sup, sent = farm
    call(sup)
    sup.emergency_stop("operator stop")
    sup.gateway.dispatch_outbound(sup.boundary)
    assert not sent


def test_boundary_failure_propagates_and_no_send_occurs(farm, monkeypatch):
    sup, sent = farm
    call(sup)
    def fail():
        raise SafetyError("check failure")
    monkeypatch.setattr(sup.boundary, "check", fail)
    with pytest.raises(SafetyError):
        sup.gateway.dispatch_outbound(sup.boundary)
    with pytest.raises(SafetyError):
        sup.tick()
    assert not sent


def test_registry_cannot_expand_after_startup(farm):
    with pytest.raises(ValueError, match="operator"):
        farm[0].registry.register(ToolAdapter())


def test_approval_bound_to_fixed_payee_and_adapter(farm):
    sup, _ = farm
    result, _ = call(sup, 1000)
    approval = result.output["approval_id"]
    sup.resolve_approval(approval, granted=True, operator="Alice")
    sup.registry.get("real.pay").spec["payee"] = "different-operator-payee"
    assert not call(sup, 1000, approval)[0].ok


def test_safety_failure_in_model_step_aborts_tick(farm, monkeypatch):
    sup, _ = farm
    def fail(*args):
        raise SafetyError("step boundary check failed")
    monkeypatch.setattr(sup, "_complete_step", fail)
    with pytest.raises(SafetyError, match="step boundary"):
        sup.tick()


@pytest.mark.parametrize("url", ["https://example.com/x", "file:///tmp/x", "http://user:pass@192.0.2.1/x",
                                  "http://192.0.2.1/x?url=other", "https://[::1]/x"])
def test_invalid_endpoint(url):
    with pytest.raises(SafetyError):
        endpoint(url)


def test_manifest_has_no_defaults():
    with pytest.raises(SafetyError):
        validate_manifest({})
    m = {"version": 1, "operator": "Alice", "approval_reference": "approval-1",
         "model_url": None, "adapters": [dict(SPEC)]}
    assert validate_manifest(m) == {("192.0.2.10", 443)}
    for key in ("payee", "hard_cap_cents", "credential_file"):
        copy = {**m, "adapters": [{k: v for k, v in SPEC.items() if k != key}]}
        with pytest.raises(SafetyError):
            validate_manifest(copy)


def test_production_supervisor_refuses_unconfirmed_boundary(tmp_path, monkeypatch):
    monkeypatch.setattr("supervisor.safety.boundary.NetworkBoundary", RealBoundary)
    with pytest.raises(SafetyError):
        make_supervisor(tmp_path)


@pytest.mark.parametrize("error", [None, errno.ECONNREFUSED, errno.ETIMEDOUT, errno.ENETUNREACH])
def test_canary_requires_kernel_rejection(monkeypatch, error):
    boundary = RealBoundary.__new__(RealBoundary)
    boundary.evidence = {"namespace": "net:[123]", "canary": ["192.0.2.1", 443]}
    monkeypatch.setattr("os.readlink", lambda _: "net:[123]")
    class Socket:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def settimeout(self, timeout): pass
        def connect(self, destination):
            if error is not None:
                raise OSError(error, "test")
    monkeypatch.setattr(socket, "socket", lambda *args: Socket())
    with pytest.raises(SafetyError):
        boundary.check()


@pytest.mark.parametrize("event", [EventType.ACCESS_APPROVED, EventType.OUTBOUND_QUEUED,
                                  EventType.OUTBOUND_ATTEMPTED, EventType.NETWORK_BARRIER_VERIFIED])
def test_agents_cannot_author_access_or_dispatch_evidence(event):
    with pytest.raises(AuthorshipError):
        check_authorship(event, "agent:malicious")


def test_model_code_remains_text_and_cannot_run(tmp_path):
    sup = make_supervisor(tmp_path)
    try:
        marker = tmp_path / "executed"
        code = "__import__('pathlib').Path(" + repr(str(marker)) + ").touch()"
        agent = sup.state.active_agents()[0]
        gen = sup.state.current_generation
        step = StepContext(agent.id, agent.lineage_id, gen, "data-step", sup.clock.tick,
                           sup.clock.now_dt(), sup.workspace_for(agent.id))
        assert sup.gateway.invoke(sup.token_for(agent.id, gen), "memory.note", {"text": code}, step).ok
        assert not marker.exists()
        assert not sup.gateway.invoke(sup.token_for(agent.id, gen), "shell", {"command": code}, step).ok
        assert not marker.exists()
    finally:
        sup.close()
