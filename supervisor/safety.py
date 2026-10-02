"""Sealed safety boundary for real-world Qwenomatic operation.

Simulation can run without a network namespace because its external systems
are deterministic in-process fixtures. Strict real-world mode is different:
the supervisor will not start population work until all of these are true:

* the current safety-relevant configuration has an operator-authored approval
  receipt in the append-only ledger;
* the separately deployed safety gateway reports the exact approved adapter
  configuration;
* direct outbound TCP from the Qwenomatic process is actually blocked; and
* the local model is reached through that gateway rather than an arbitrary
  model URL.

This module contains no agent-facing mutation surface. Population agents
cannot call it, change its configuration, or see credentials.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import urllib.parse
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from storage.events import EventStore, EventType, digest


class SafetyBoundaryError(RuntimeError):
    """Strict operation cannot prove the required safety boundary."""


@contextmanager
def exclusive_operator_lock(data_dir: Any):
    """Take the same OS lock as Supervisor without starting population code."""
    path = data_dir / "supervisor.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)
    fh = open(path, "r+", encoding="utf-8")
    if os.name == "nt":
        import msvcrt
        fh.seek(0, os.SEEK_END)
        if fh.tell() == 0:
            fh.write(" ")
            fh.flush()
        fh.seek(0)
        try:
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            fh.close()
            raise SafetyBoundaryError("stop the running supervisor before approving safety configuration") from exc
        try:
            yield
        finally:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            fh.close()
    else:
        import fcntl
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            fh.close()
            raise SafetyBoundaryError("stop the running supervisor before approving safety configuration") from exc
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
            fh.close()


CONTROL_PLANE_KEYS = frozenset(
    {
        "url", "uri", "host", "hostname", "endpoint", "destination", "route",
        "payee", "recipient", "recipient_account", "account", "account_number",
        "routing_number", "iban", "wallet",
        "credential", "credentials", "token", "api_key", "secret", "password",
        "command", "cmd", "shell", "script", "code", "query", "sql", "executable", "exec",
    }
)


def _key(k: Any) -> str:
    return str(k).strip().lower().replace("-", "_").replace(" ", "_")


def control_plane_paths(value: Any, prefix: str = "") -> list[str]:
    """Return agent-supplied paths that attempt to choose trusted control-plane values."""
    bad: list[str] = []
    if isinstance(value, dict):
        for raw, child in value.items():
            key = _key(raw)
            path = f"{prefix}.{key}" if prefix else key
            if key in CONTROL_PLANE_KEYS:
                bad.append(path)
            bad.extend(control_plane_paths(child, path))
    elif isinstance(value, list):
        for i, child in enumerate(value):
            bad.extend(control_plane_paths(child, f"{prefix}[{i}]"))
    return bad


def adapter_config_hash(adapters: dict[str, Any]) -> str:
    return digest(adapters or {"version": 1, "adapters": {}})


def safety_fingerprint(config: Any) -> str:
    """Fingerprint every setting whose change can widen real-world authority."""
    farm = config.farm
    inference = farm.get("inference", {})
    safety = farm.get("safety", {})
    economy = farm.get("economy", {})
    return digest(
        {
            "policy": config.policy,
            "adapters": config.adapters,
            "safety": safety,
            "inference_route": {
                "backend": inference.get("backend"),
                "base_url": (inference.get("openai_compatible") or {}).get("base_url"),
                "local": (inference.get("openai_compatible") or {}).get("local", True),
            },
            "economy_authority": {
                "currency": economy.get("currency"),
                "farm_controlled_accounts": economy.get("farm_controlled_accounts", []),
            },
        }
    )


def strict_mode(config: Any) -> bool:
    return str((config.farm.get("safety") or {}).get("mode", "simulation")).lower() == "strict"


def latest_approved_fingerprint(store: EventStore) -> str | None:
    rows = store.iter_events(types=[EventType.SAFETY_CONFIG_APPROVED], descending=True, limit=1)
    return str(rows[0].payload.get("fingerprint")) if rows else None


def record_operator_approval(store: EventStore, config: Any, *, operator: str, note: str = "") -> str:
    """Record an explicit human decision to approve the current sealed authority."""
    fp = safety_fingerprint(config)
    store.append(
        EventType.SAFETY_CONFIG_APPROVED,
        {
            "fingerprint": fp,
            "config_hashes": config.hashes(),
            "adapter_ids": sorted((config.adapters.get("adapters") or {}).keys()),
            "note": note,
        },
        author=f"operator:{operator}",
        idempotency_key=f"safety-config-approved:{fp}",
    )
    return fp


@dataclass(frozen=True)
class SafetyAttestation:
    fingerprint: str
    gateway_url: str
    adapter_config_hash: str
    forbidden_probe: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "gateway_url": self.gateway_url,
            "adapter_config_hash": self.adapter_config_hash,
            "forbidden_probe": self.forbidden_probe,
        }


def _gateway_health(url: str, timeout: float) -> dict[str, Any]:
    req = urllib.request.Request(url.rstrip("/") + "/healthz", headers={"User-Agent": "qwenomatic-safety"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        if response.status != 200:
            raise SafetyBoundaryError(f"safety gateway health returned HTTP {response.status}")
        data = json.loads(response.read(1 << 20) or b"{}")
    if not isinstance(data, dict) or data.get("ok") is not True or data.get("mode") != "sealed":
        raise SafetyBoundaryError("safety gateway did not attest sealed mode")
    return data


def _prove_forbidden_egress(host: str, port: int, timeout: float) -> None:
    """The proof is the failed connect itself, not a configuration flag claiming a block."""
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except OSError:
        return
    try:
        raise SafetyBoundaryError(f"forbidden egress probe unexpectedly reached {host}:{port}")
    finally:
        sock.close()


def attest_network_boundary(config: Any, store: EventStore) -> SafetyAttestation | None:
    """Fail closed in strict mode unless the external boundary is demonstrably in place."""
    if not strict_mode(config):
        return None

    safety = config.farm.get("safety") or {}
    fp = safety_fingerprint(config)
    approved = latest_approved_fingerprint(store)
    if approved != fp:
        raise SafetyBoundaryError(
            "current safety configuration is not operator-approved; run qwenomatic safety-approve while stopped"
        )

    gateway_url = str(safety.get("gateway_url") or "").rstrip("/")
    parsed = urllib.parse.urlsplit(gateway_url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise SafetyBoundaryError("strict mode requires safety.gateway_url")

    inference = config.farm.get("inference") or {}
    if inference.get("backend") == "openai_compatible":
        model_url = str((inference.get("openai_compatible") or {}).get("base_url") or "").rstrip("/")
        required = gateway_url + "/model/v1"
        if model_url != required:
            raise SafetyBoundaryError(f"strict model route must be {required}, not {model_url or '<empty>'}")

    timeout = float(safety.get("attestation_timeout_seconds", 2.0))
    health = _gateway_health(gateway_url, timeout)
    want_adapters = adapter_config_hash(config.adapters)
    if health.get("adapter_config_hash") != want_adapters:
        raise SafetyBoundaryError("safety gateway adapter configuration does not match the approved supervisor config")
    model_upstream = str(safety.get("model_upstream") or "").rstrip("/")
    if not model_upstream:
        raise SafetyBoundaryError("strict mode requires safety.model_upstream")
    if health.get("model_upstream_hash") != hashlib.sha256(model_upstream.encode()).hexdigest():
        raise SafetyBoundaryError("safety gateway model upstream differs from the approved supervisor config")
    if health.get("safety_fingerprint") != fp or health.get("credentials_ok") is not True:
        raise SafetyBoundaryError("safety gateway did not attest the approved policy/credentials")

    probe = safety.get("forbidden_probe") or {}
    host = str(probe.get("host") or "1.1.1.1")
    port = int(probe.get("port") or 443)
    _prove_forbidden_egress(host, port, float(probe.get("timeout_seconds", timeout)))

    attestation = SafetyAttestation(
        fingerprint=fp,
        gateway_url=gateway_url,
        adapter_config_hash=want_adapters,
        forbidden_probe=f"{host}:{port}",
    )
    store.append(
        EventType.SAFETY_ATTESTED,
        attestation.to_dict(),
        idempotency_key=f"safety-attested:{fp}:{store.head()[0] + 1}",
    )
    return attestation
