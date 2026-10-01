"""Trusted client for one statically bound broker route.

Real-world tool adapters may use this class, but population agents never receive
it as a tool. The route id is fixed when the adapter is constructed; requests
cannot supply a host, URL, credential or payee.
"""

from __future__ import annotations

import http.client
import json
import socket
from typing import Any


class EgressError(RuntimeError):
    pass


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str, timeout: float) -> None:
        super().__init__("qwenomatic-broker", timeout=timeout)
        self.socket_path = socket_path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.socket_path)


class FixedRouteClient:
    def __init__(self, socket_path: str, route_id: str, *, timeout: float = 30.0) -> None:
        if not route_id or "/" in route_id or ".." in route_id:
            raise ValueError("invalid fixed route id")
        self.socket_path = socket_path
        self.route_id = route_id
        self.timeout = timeout

    def request_json(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> tuple[int, Any]:
        if not path.startswith("/") or "://" in path:
            raise ValueError("path must be relative to the adapter's fixed destination")
        body = json.dumps(payload).encode() if payload is not None else None
        headers = {"Content-Type": "application/json"} if body is not None else {}
        conn = _UnixConnection(self.socket_path, self.timeout)
        try:
            conn.request(method.upper(), f"/route/{self.route_id}{path}", body=body, headers=headers)
            response = conn.getresponse()
            raw = response.read()
        except (OSError, TimeoutError, http.client.HTTPException) as exc:
            raise EgressError(str(exc)) from exc
        finally:
            conn.close()
        try:
            parsed = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            parsed = raw.decode("utf-8", "replace")
        return response.status, parsed
