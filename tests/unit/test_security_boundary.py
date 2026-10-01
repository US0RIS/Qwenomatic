from pathlib import Path

import pytest

from runtime.tools.base import ToolAdapter, ToolRegistry
from storage.events import AuthorshipError, EventStore, EventType, agent_author
from supervisor.security import NetworkBoundaryError, NetworkGuard, SecurityRegistry, SecurityScopeError


def _security_config():
    return {
        "network": {
            "mode": "container_none",
            "required_for_backends": ["openai_compatible"],
            "required_for_real_world": True,
            "broker_socket": "/run/qwenomatic/egress.sock",
            "blocked_probe": {"host": "1.1.1.1", "port": 443, "timeout_seconds": 0.01},
        },
        "destinations": {
            "local_model": {
                "scheme": "http",
                "host": "host.docker.internal",
                "port": 11434,
                "base_path": "/v1",
            },
            "merchant": {"scheme": "https", "host": "api.example.test", "port": 443, "credential": "merchant_key"},
        },
        "credentials": {
            "merchant_key": {"env_var": "MERCHANT_KEY", "header": "Authorization", "prefix": "Bearer "}
        },
        "payees": {"vendor-a": {"label": "Vendor A"}},
        "adapters": {
            "merchant.charge": {
                "destination": "merchant",
                "credential": "merchant_key",
                "payee": "vendor-a",
                "hard_cap": 50.0,
                "approval_threshold": 10.0,
            }
        },
    }


@pytest.fixture
def store(tmp_path):
    s = EventStore(tmp_path / "ledger.sqlite3")
    yield s
    s.close()


def test_agent_cannot_authorize_or_revoke_security_scope(store):
    payload = {
        "kind": "destination",
        "scope_id": "local_model",
        "scope_digest": "forged",
        "operator": "agent",
    }
    with pytest.raises(AuthorshipError):
        store.append(EventType.SECURITY_SCOPE_APPROVED, payload, author=agent_author("attacker"))
    with pytest.raises(AuthorshipError):
        store.append(EventType.SECURITY_SCOPE_REVOKED, payload, author=agent_author("attacker"))


def test_config_is_only_a_proposal_and_exact_definition_approval_is_required(store):
    cfg = _security_config()
    registry = SecurityRegistry(cfg, store)
    assert not registry.is_approved("destination", "local_model")

    registry.approve("destination", "local_model", operator="tester")
    assert registry.is_approved("destination", "local_model")

    changed = _security_config()
    changed["destinations"]["local_model"]["port"] = 9999
    assert not SecurityRegistry(changed, store).is_approved("destination", "local_model")

    registry.revoke("destination", "local_model", operator="tester")
    assert not registry.is_approved("destination", "local_model")
    registry.approve("destination", "local_model", operator="tester")
    assert registry.is_approved("destination", "local_model")


def test_manifest_contains_only_operator_approved_destinations(store):
    cfg = _security_config()
    registry = SecurityRegistry(cfg, store)
    registry.approve("destination", "local_model", operator="tester")
    manifest = registry.active_manifest()
    assert set(manifest["destinations"]) == {"local_model"}
    assert "manifest_digest" in manifest


def test_real_world_adapter_cannot_take_dynamic_destination_or_code_fields(store):
    cfg = _security_config()
    registry = SecurityRegistry(cfg, store)

    class Bad(ToolAdapter):
        name = "merchant.charge"
        real_world = True
        destination_id = "merchant"
        args_schema = {"url": "str"}

    with pytest.raises(SecurityScopeError, match="forbidden dynamic"):
        registry.validate_adapter(Bad())


def test_real_world_adapter_can_register_as_inert_before_operator_activation(store):
    cfg = _security_config()
    registry = SecurityRegistry(cfg, store)

    class Charge(ToolAdapter):
        name = "merchant.charge"
        real_world = True
        destination_id = "merchant"
        credential_id = "merchant_key"
        outbound_payment = True
        fixed_payee_id = "vendor-a"
        hard_spend_cap = 50.0
        approval_threshold = 10.0
        args_schema = {"amount": "number"}

    ToolRegistry(security=registry).register(Charge())
    with pytest.raises(SecurityScopeError, match="not operator-approved"):
        registry.validate_adapter(Charge(), require_approval=True)


def test_payment_adapter_requires_approved_fixed_route_credential_payee_and_caps(store):
    cfg = _security_config()
    registry = SecurityRegistry(cfg, store)
    for kind, scope_id in [
        ("destination", "merchant"),
        ("credential", "merchant_key"),
        ("payee", "vendor-a"),
        ("adapter", "merchant.charge"),
    ]:
        registry.approve(kind, scope_id, operator="tester")

    class Charge(ToolAdapter):
        name = "merchant.charge"
        real_world = True
        destination_id = "merchant"
        credential_id = "merchant_key"
        outbound_payment = True
        fixed_payee_id = "vendor-a"
        hard_spend_cap = 50.0
        approval_threshold = 10.0
        args_schema = {"amount": "number"}

    adapter = Charge()
    registry.validate_adapter(adapter)
    assert registry.action_requires_approval(adapter, 10.01)
    assert not registry.action_requires_approval(adapter, 10.0)
    assert registry.action_exceeds_hard_cap(adapter, 50.01)
    assert not registry.action_exceeds_hard_cap(adapter, 50.0)


def test_tool_registry_rejects_agent_code_execution_even_for_non_network_tool():
    class Executor(ToolAdapter):
        name = "bad.exec"
        executes_agent_code = True

    with pytest.raises(ValueError, match="execute agent-authored code"):
        ToolRegistry().register(Executor())


def test_real_world_tool_cannot_register_without_security_registry():
    class External(ToolAdapter):
        name = "external.fixed"
        real_world = True
        destination_id = "x"

    with pytest.raises(ValueError, match="requires a supervisor security registry"):
        ToolRegistry().register(External())


def test_network_isolation_cannot_be_disabled_by_config(store):
    cfg = _security_config()
    cfg["network"]["required_for_backends"] = []
    cfg["network"]["required_for_real_world"] = False
    guard = NetworkGuard(cfg, SecurityRegistry(cfg, store))
    assert guard.required("openai_compatible")
    assert guard.required("anything_external")
    assert not guard.required("simulated")
    assert guard.required("simulated", has_real_world_adapters=True)


def test_network_guard_is_fail_closed_without_container_attestation(store, monkeypatch):
    registry = SecurityRegistry(_security_config(), store)
    guard = NetworkGuard(_security_config(), registry)
    monkeypatch.delenv("QWENOMATIC_NETWORK_MODE", raising=False)
    with pytest.raises(NetworkBoundaryError, match="network isolation is required"):
        guard.verify("openai_compatible")


def test_simulated_backend_does_not_need_os_network_when_no_real_world_adapters(store):
    registry = SecurityRegistry(_security_config(), store)
    guard = NetworkGuard(_security_config(), registry)
    assert guard.verify("simulated") is None
