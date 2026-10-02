"""Declarative real-world adapters routed through the sealed safety gateway.

Population agents choose only business arguments allowed by a schema. They do
not choose the adapter destination, HTTP method, payee, account, credential,
command, query, or code. Those live in operator-approved adapters.yaml.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from runtime.tools.base import ToolContext

from ..safety import CONTROL_PLANE_KEYS

_ID = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ARG_TYPES = frozenset({"str", "number", "int", "bool"})


class AdapterConfigError(ValueError):
    pass


def validate_adapter_specs(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if not isinstance(config, dict):
        raise AdapterConfigError("adapters config must be an object")
    raw = config.get("adapters") or {}
    if not isinstance(raw, dict):
        raise AdapterConfigError("adapters must be a mapping")
    out: dict[str, dict[str, Any]] = {}
    tools: set[str] = set()
    for adapter_id, value in raw.items():
        if not _ID.fullmatch(str(adapter_id)):
            raise AdapterConfigError(f"invalid adapter id {adapter_id!r}")
        if not isinstance(value, dict):
            raise AdapterConfigError(f"adapter {adapter_id}: config must be an object")
        spec = dict(value)
        tool = str(spec.get("tool") or "")
        if not _ID.fullmatch(tool):
            raise AdapterConfigError(f"adapter {adapter_id}: invalid tool name")
        if tool in tools:
            raise AdapterConfigError(f"adapter {adapter_id}: duplicate tool {tool}")
        tools.add(tool)
        if not str(spec.get("action_class") or ""):
            raise AdapterConfigError(f"adapter {adapter_id}: action_class is required")

        schema = spec.get("args_schema") or {}
        optional = set(spec.get("optional_args") or [])
        if not isinstance(schema, dict) or any(v not in _ARG_TYPES for v in schema.values()):
            raise AdapterConfigError(f"adapter {adapter_id}: args_schema uses unsupported types")
        bad = [k for k in schema if str(k).lower().replace("-", "_") in CONTROL_PLANE_KEYS]
        if bad:
            raise AdapterConfigError(
                f"adapter {adapter_id}: agent schema exposes supervisor-owned field(s): {', '.join(map(str, bad))}"
            )
        if not optional <= set(schema):
            raise AdapterConfigError(f"adapter {adapter_id}: optional_args must be in args_schema")

        destination = spec.get("destination") or {}
        url = str(destination.get("url") or "")
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
            raise AdapterConfigError(f"adapter {adapter_id}: destination must be one fixed HTTP(S) URL")
        if parsed.query or parsed.fragment:
            raise AdapterConfigError(f"adapter {adapter_id}: destination URL cannot contain query/fragment")
        if parsed.scheme != "https" and not bool(destination.get("allow_insecure")):
            raise AdapterConfigError(f"adapter {adapter_id}: external destination must use HTTPS")
        method = str(destination.get("method") or "POST").upper()
        if method not in ("POST", "PUT", "PATCH"):
            raise AdapterConfigError(f"adapter {adapter_id}: method must be POST, PUT or PATCH")

        credential = spec.get("credential")
        if credential:
            if not isinstance(credential, dict) or not credential.get("file"):
                raise AdapterConfigError(f"adapter {adapter_id}: credential requires a file")
            expected = str(credential.get("sha256") or "").lower()
            if not _SHA256.fullmatch(expected):
                raise AdapterConfigError(f"adapter {adapter_id}: credential sha256 is required")
            if not str(credential.get("header") or "Authorization"):
                raise AdapterConfigError(f"adapter {adapter_id}: credential header is required")

        kind = str(spec.get("kind") or "http")
        if kind not in ("http", "payment"):
            raise AdapterConfigError(f"adapter {adapter_id}: kind must be http or payment")
        if kind == "payment":
            payment = spec.get("payment") or {}
            amount_field = str(payment.get("amount_field") or "amount")
            if schema.get(amount_field) != "number":
                raise AdapterConfigError(f"adapter {adapter_id}: payment amount field must be a number argument")
            if not str(payment.get("payee") or ""):
                raise AdapterConfigError(f"adapter {adapter_id}: payment payee must be fixed in config")
            currency = str(payment.get("currency") or "")
            if not re.fullmatch(r"[A-Z]{3}", currency):
                raise AdapterConfigError(f"adapter {adapter_id}: payment currency must be a fixed ISO code")
            hard_cap = float(payment.get("hard_cap_per_action") or 0)
            material = float(payment.get("material_threshold") or 0)
            if hard_cap <= 0:
                raise AdapterConfigError(f"adapter {adapter_id}: payment hard_cap_per_action must be positive")
            if material < 0 or material > hard_cap:
                raise AdapterConfigError(f"adapter {adapter_id}: material_threshold must be within the hard cap")
        out[str(adapter_id)] = spec
    return out


class ExternalGatewayClient:
    """Client for the only network-reachable process in the farm namespace."""

    def __init__(self, gateway_url: str, *, timeout: float = 30.0) -> None:
        self.gateway_url = gateway_url.rstrip("/")
        parsed = urllib.parse.urlsplit(self.gateway_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise AdapterConfigError("safety gateway URL is invalid")
        self.timeout = timeout

    def invoke(self, adapter_id: str, tool: str, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        body = {
            "adapter_id": adapter_id,
            "tool": tool,
            "args": args,
            "invocation_id": ctx.invocation_id,
            "agent_id": ctx.agent_id,
            "generation_id": ctx.generation_id,
            "capability_epoch": ctx.capability_epoch,
            "policy_fingerprint": ctx.policy_fingerprint,
            "approval_id": ctx.approval_id,
        }
        req = urllib.request.Request(
            f"{self.gateway_url}/adapter/{urllib.parse.quote(adapter_id, safe='')}/invoke",
            data=json.dumps(body, separators=(",", ":")).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Capability {ctx.capability_token}",
                "User-Agent": "qwenomatic-supervisor",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                data = json.loads(response.read(1 << 20) or b"{}")
        except urllib.error.HTTPError as exc:
            try:
                data = json.loads(exc.read(1 << 20) or b"{}")
            except ValueError:
                data = {"ok": False, "state": "uncertain" if exc.code >= 500 else "rejected",
                        "error": f"safety gateway HTTP {exc.code}"}
        except (OSError, TimeoutError, ValueError):
            return {"ok": False, "state": "uncertain", "error": "safety gateway transport failure"}
        if not isinstance(data, dict):
            return {"ok": False, "state": "uncertain", "error": "invalid safety gateway response"}
        return data
