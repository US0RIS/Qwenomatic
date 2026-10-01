"""Fail-closed runtime verification for the OS/container network barrier."""

from __future__ import annotations

import json
import os
import socket
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .registry import SecurityRegistry


class NetworkBoundaryError(RuntimeError):
    pass


@dataclass(frozen=True)
class NetworkAttestation:
    mode: str
    interfaces: tuple[str, ...]
    blocked_probe: str
    broker_socket: str
    manifest_digest: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "interfaces": list(self.interfaces),
            "blocked_probe": self.blocked_probe,
            "broker_socket": self.broker_socket,
            "manifest_digest": self.manifest_digest,
        }


def _unix_http_get(socket_path: str, path: str, timeout: float = 2.0) -> tuple[int, bytes]:
    request = f"GET {path} HTTP/1.1\r\nHost: qwenomatic-broker\r\nConnection: close\r\n\r\n".encode()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(socket_path)
        s.sendall(request)
        chunks: list[bytes] = []
        while True:
            b = s.recv(65536)
            if not b:
                break
            chunks.append(b)
    raw = b"".join(chunks)
    head, sep, body = raw.partition(b"\r\n\r\n")
    if not sep:
        raise NetworkBoundaryError("egress broker returned malformed HTTP")
    first = head.splitlines()[0].decode("ascii", "replace").split()
    if len(first) < 2 or not first[1].isdigit():
        raise NetworkBoundaryError("egress broker returned malformed status")
    return int(first[1]), body


class NetworkGuard:
    """Prove that executable farm code is in a network-less container.

    The farm container is expected to use Docker/OCI network_mode=none.
    Its only egress primitive is an AF_UNIX socket shared with a separate,
    trusted broker container. The broker exposes named, fixed routes from an
    operator-approved manifest and has no arbitrary URL or CONNECT endpoint.
    """

    def __init__(self, config: dict[str, Any], registry: SecurityRegistry) -> None:
        self.config = config or {}
        self.registry = registry

    def required(self, backend_name: str, *, has_real_world_adapters: bool = False) -> bool:
        # This boundary is deliberately not a config switch. Configuration may
        # describe the barrier, but it cannot turn the barrier off. Only the
        # deterministic in-process simulator is exempt because it has no
        # external model or real-world I/O.
        return backend_name != "simulated" or has_real_world_adapters

    def verify(
        self, backend_name: str, *, has_real_world_adapters: bool = False
    ) -> NetworkAttestation | None:
        if not self.required(backend_name, has_real_world_adapters=has_real_world_adapters):
            return None
        net = self.config.get("network") or {}
        expected_mode = str(net.get("mode", "container_none"))
        if expected_mode != "container_none":
            raise NetworkBoundaryError(f"unsupported required network mode {expected_mode!r}")
        if os.environ.get("QWENOMATIC_NETWORK_MODE") != "container_none":
            raise NetworkBoundaryError(
                "network isolation is required but not attested; launch with scripts/secure_run.ps1 "
                "or docker compose -f docker-compose.secure.yml"
            )

        sys_net = Path("/sys/class/net")
        if not sys_net.is_dir():
            raise NetworkBoundaryError(
                "cannot inspect container network interfaces; refusing unverified execution"
            )
        interfaces = tuple(sorted(p.name for p in sys_net.iterdir()))
        if interfaces != ("lo",):
            raise NetworkBoundaryError(
                f"container has routable network interfaces {interfaces!r}; expected only loopback"
            )

        probe = net.get("blocked_probe") or {"host": "1.1.1.1", "port": 443}
        probe_host = str(probe.get("host", "1.1.1.1"))
        probe_port = int(probe.get("port", 443))
        timeout = float(probe.get("timeout_seconds", 0.5))
        try:
            conn = socket.create_connection((probe_host, probe_port), timeout=timeout)
        except OSError:
            pass
        else:
            conn.close()
            raise NetworkBoundaryError(
                f"unapproved outbound probe unexpectedly succeeded to {probe_host}:{probe_port}"
            )

        broker_socket = str(net.get("broker_socket", "/run/qwenomatic/egress.sock"))
        p = Path(broker_socket)
        try:
            mode = p.stat().st_mode
        except OSError as exc:
            raise NetworkBoundaryError(
                f"egress broker socket unavailable: {broker_socket}: {exc}"
            ) from exc
        if not stat.S_ISSOCK(mode):
            raise NetworkBoundaryError(f"egress broker path is not a Unix socket: {broker_socket}")

        expected_digest = self.registry.manifest_digest()
        try:
            status, body = _unix_http_get(broker_socket, "/__health")
            payload = json.loads(body)
        except Exception as exc:
            raise NetworkBoundaryError(f"cannot verify egress broker: {exc}") from exc
        if status != 200:
            raise NetworkBoundaryError(f"egress broker health returned HTTP {status}")
        actual_digest = str(payload.get("manifest_digest", ""))
        if actual_digest != expected_digest:
            raise NetworkBoundaryError(
                "egress broker manifest does not match the ledger-approved security scopes"
            )

        return NetworkAttestation(
            mode="container_none",
            interfaces=interfaces,
            blocked_probe=f"{probe_host}:{probe_port}",
            broker_socket=broker_socket,
            manifest_digest=actual_digest,
        )
