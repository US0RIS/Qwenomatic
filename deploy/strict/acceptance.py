"""Executable proof of the strict network boundary from inside the farm."""

from __future__ import annotations

import json
import socket
import urllib.request


def must_connect(name: str, host: str, port: int) -> None:
    try:
        s = socket.create_connection((host, port), timeout=3)
    except OSError as exc:
        raise SystemExit(f"FAIL: {name} is not reachable: {exc}")
    else:
        s.close()
        print(f"PASS: {name} reachable")


def must_not_connect(name: str, host: str, port: int) -> None:
    try:
        s = socket.create_connection((host, port), timeout=2)
    except OSError:
        print(f"PASS: {name} blocked")
        return
    s.close()
    raise SystemExit(f"FAIL: {name} unexpectedly reachable at {host}:{port}")


must_connect("safety gateway", "safety-gateway", 8787)
with urllib.request.urlopen("http://safety-gateway:8787/healthz", timeout=3) as r:
    health = json.loads(r.read())
if health.get("ok") is not True or health.get("mode") != "sealed":
    raise SystemExit("FAIL: gateway did not attest sealed mode")
print("PASS: gateway attests sealed mode")

# These are direct socket probes from the same container/process namespace in
# which the agents run. The test succeeds only when networking actually denies
# them; it does not inspect or trust a configuration flag.
must_not_connect("unapproved public egress", "1.1.1.1", 443)
must_not_connect("direct local-model bypass", "host.docker.internal", 11434)

# The approved model remains usable, but only through the gateway.
try:
    with urllib.request.urlopen("http://safety-gateway:8787/model/v1/models", timeout=10) as r:
        if r.status != 200:
            raise SystemExit(f"FAIL: brokered model returned HTTP {r.status}")
except Exception as exc:
    raise SystemExit(f"FAIL: brokered local model is not reachable: {exc}") from exc
print("PASS: approved local model reachable only through broker")
print("STRICT SAFETY ACCEPTANCE: PASS")
