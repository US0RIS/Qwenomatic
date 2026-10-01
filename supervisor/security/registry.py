"""Operator-approved real-world security scopes.

Configuration proposes scopes; it never activates them. A scope becomes usable
only when an operator approval event for the exact current definition exists in
the append-only ledger. Editing config therefore invalidates the old approval.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from storage.events import EventStore, EventType
from storage.events.canonical import digest


class SecurityScopeError(RuntimeError):
    pass


_SECTIONS = {
    "destination": "destinations",
    "credential": "credentials",
    "payee": "payees",
    "adapter": "adapters",
}


@dataclass(frozen=True)
class ScopeStatus:
    kind: str
    scope_id: str
    digest: str
    approved: bool
    definition: dict[str, Any]


class SecurityRegistry:
    """Resolve exact-definition operator approvals from config + ledger."""

    def __init__(self, config: dict[str, Any], store: EventStore) -> None:
        self.config = config or {}
        self.store = store

    def definition(self, kind: str, scope_id: str) -> dict[str, Any]:
        section = _SECTIONS.get(kind)
        if section is None:
            raise SecurityScopeError(f"unknown security scope kind {kind!r}")
        value = (self.config.get(section) or {}).get(scope_id)
        if not isinstance(value, dict):
            raise SecurityScopeError(f"no configured {kind} scope {scope_id!r}")
        return value

    def scope_digest(self, kind: str, scope_id: str) -> str:
        return digest({"kind": kind, "id": scope_id, "definition": self.definition(kind, scope_id)})

    def _decisions(self) -> dict[tuple[str, str], tuple[bool, str]]:
        decisions: dict[tuple[str, str], tuple[bool, str]] = {}
        events = self.store.iter_events(
            types=[EventType.SECURITY_SCOPE_APPROVED, EventType.SECURITY_SCOPE_REVOKED]
        )
        for event in events:
            p = event.payload
            key = (str(p.get("kind")), str(p.get("scope_id")))
            if event.type is EventType.SECURITY_SCOPE_APPROVED:
                decisions[key] = (True, str(p.get("scope_digest", "")))
            else:
                decisions[key] = (False, str(p.get("scope_digest", "")))
        return decisions

    def status(self, kind: str, scope_id: str) -> ScopeStatus:
        definition = self.definition(kind, scope_id)
        current = self.scope_digest(kind, scope_id)
        approved, approved_digest = self._decisions().get((kind, scope_id), (False, ""))
        return ScopeStatus(kind, scope_id, current, bool(approved and approved_digest == current), definition)

    def is_approved(self, kind: str, scope_id: str) -> bool:
        return self.status(kind, scope_id).approved

    def require_approved(self, kind: str, scope_id: str) -> dict[str, Any]:
        status = self.status(kind, scope_id)
        if not status.approved:
            raise SecurityScopeError(
                f"{kind} {scope_id!r} is not operator-approved for its current definition; "
                f"run qwenomatic security approve {kind} {scope_id}"
            )
        return status.definition

    def approve(self, kind: str, scope_id: str, *, operator: str, note: str = "") -> None:
        definition = self.definition(kind, scope_id)
        scope_digest = self.scope_digest(kind, scope_id)
        self.store.append(
            EventType.SECURITY_SCOPE_APPROVED,
            {
                "kind": kind,
                "scope_id": scope_id,
                "scope_digest": scope_digest,
                "definition_digest": digest(definition),
                "operator": operator,
                "note": note,
            },
            author=f"operator:{operator}",
        )

    def revoke(self, kind: str, scope_id: str, *, operator: str, note: str = "") -> None:
        scope_digest = self.scope_digest(kind, scope_id)
        self.store.append(
            EventType.SECURITY_SCOPE_REVOKED,
            {
                "kind": kind,
                "scope_id": scope_id,
                "scope_digest": scope_digest,
                "operator": operator,
                "note": note,
            },
            author=f"operator:{operator}",
        )

    def list_status(self) -> list[ScopeStatus]:
        out: list[ScopeStatus] = []
        for kind, section in _SECTIONS.items():
            for scope_id in sorted((self.config.get(section) or {})):
                out.append(self.status(kind, scope_id))
        return out

    def active_manifest(self) -> dict[str, Any]:
        """Return the deterministic broker manifest for approved destinations."""
        destinations: dict[str, Any] = {}
        for destination_id in sorted((self.config.get("destinations") or {})):
            if not self.is_approved("destination", destination_id):
                continue
            d = dict(self.definition("destination", destination_id))
            credential_id = d.get("credential")
            credential: dict[str, Any] | None = None
            if credential_id is not None:
                credential = dict(self.require_approved("credential", str(credential_id)))
                allowed = {"env_var", "header", "prefix"}
                credential = {k: credential[k] for k in allowed if k in credential}
                credential["id"] = str(credential_id)
            destinations[destination_id] = {
                "scheme": str(d.get("scheme", "https")),
                "host": str(d["host"]),
                "port": int(d.get("port", 443 if d.get("scheme", "https") == "https" else 80)),
                "base_path": str(d.get("base_path", "")),
                "credential": credential,
            }
        core = {"version": 1, "destinations": destinations}
        return {**core, "manifest_digest": digest(core)}

    def manifest_digest(self) -> str:
        return str(self.active_manifest()["manifest_digest"])

    def validate_adapter(self, adapter: Any, *, require_approval: bool = True) -> None:
        """Validate a real-world adapter's static contract and, at execution time,
        its exact operator-approved bindings.

        Registration checks structure against proposed config without activating
        anything. Execution repeats the check with require_approval=True.
        """
        if not bool(getattr(adapter, "real_world", False)):
            if bool(getattr(adapter, "outbound_payment", False)):
                raise SecurityScopeError("outbound-payment adapters must be marked real_world")
            return

        forbidden = {
            "url", "uri", "host", "hostname", "domain", "ip", "port",
            "account", "account_id", "account_number", "payee", "payee_id",
            "recipient", "destination", "destination_id", "path", "endpoint",
            "credential", "credential_id", "api_key", "token", "secret", "password",
            "auth", "authorization", "command", "cmd", "script", "code", "sql",
        }
        dynamic = forbidden.intersection(getattr(adapter, "args_schema", {}).keys())
        if dynamic:
            raise SecurityScopeError(
                f"real-world adapter {adapter.name!r} exposes forbidden dynamic routing/execution fields: "
                + ", ".join(sorted(dynamic))
            )

        get_scope = self.require_approved if require_approval else self.definition
        adapter_cfg = get_scope("adapter", adapter.name)

        destination_id = str(getattr(adapter, "destination_id", "") or "")
        if not destination_id:
            raise SecurityScopeError(f"real-world adapter {adapter.name!r} has no fixed destination_id")
        if adapter_cfg.get("destination") != destination_id:
            raise SecurityScopeError(f"adapter {adapter.name!r} destination does not match configured contract")
        destination_cfg = get_scope("destination", destination_id)

        configured_credential = adapter_cfg.get("credential")
        if configured_credential is not None:
            get_scope("credential", str(configured_credential))
        if getattr(adapter, "credential_id", None) != configured_credential:
            raise SecurityScopeError(f"adapter {adapter.name!r} credential binding does not match configured contract")
        if destination_cfg.get("credential") != configured_credential:
            raise SecurityScopeError(
                f"destination {destination_id!r} credential binding does not match adapter {adapter.name!r}"
            )

        if bool(getattr(adapter, "outbound_payment", False)):
            payee = str(getattr(adapter, "fixed_payee_id", "") or "")
            if not payee or adapter_cfg.get("payee") != payee:
                raise SecurityScopeError(f"payment adapter {adapter.name!r} must have one fixed configured payee")
            get_scope("payee", payee)
            if getattr(adapter, "args_schema", {}).get("amount") != "number":
                raise SecurityScopeError(
                    f"payment adapter {adapter.name!r} must expose only a numeric amount as its spend field"
                )
            cap = float(getattr(adapter, "hard_spend_cap", 0.0) or 0.0)
            threshold = float(getattr(adapter, "approval_threshold", 0.0) or 0.0)
            if cap <= 0:
                raise SecurityScopeError(f"payment adapter {adapter.name!r} needs a positive hard_spend_cap")
            if threshold < 0 or threshold > cap:
                raise SecurityScopeError(f"payment adapter {adapter.name!r} has invalid approval_threshold")
            if abs(cap - float(adapter_cfg.get("hard_cap", -1))) > 1e-9:
                raise SecurityScopeError(f"payment adapter {adapter.name!r} cap does not match configured contract")
            if abs(threshold - float(adapter_cfg.get("approval_threshold", -1))) > 1e-9:
                raise SecurityScopeError(f"payment adapter {adapter.name!r} approval threshold mismatch")

    def action_requires_approval(self, adapter: Any, spend: float) -> bool:
        return bool(
            getattr(adapter, "real_world", False)
            and getattr(adapter, "outbound_payment", False)
            and spend > float(getattr(adapter, "approval_threshold", 0.0) or 0.0)
        )

    def action_exceeds_hard_cap(self, adapter: Any, spend: float) -> bool:
        if not (getattr(adapter, "real_world", False) and getattr(adapter, "outbound_payment", False)):
            return False
        return spend > float(getattr(adapter, "hard_spend_cap", 0.0) or 0.0)
