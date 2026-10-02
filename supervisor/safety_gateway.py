"""Separate fixed-route safety gateway used by strict deployments.

This process is intentionally outside the population container's network
namespace. Qwenomatic can reach this gateway, but not the WAN. The gateway
accepts only two classes of traffic:

* the fixed local model upstream, through /model/v1/{models,chat/completions};
* operator-approved declarative adapters from adapters.yaml.

No request can supply a destination, payee, credential, command, script,
query, or arbitrary URL.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .config import FarmConfig
from .policy.capabilities import InvalidToken, TokenAuthority
from .policy.external import AdapterConfigError, validate_adapter_specs
from .safety import adapter_config_hash, control_plane_paths


class GatewayError(RuntimeError):
    pass


_TYPES = {"str": str, "number": (int, float), "int": int, "bool": bool}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _read_credential(spec: dict[str, Any]) -> tuple[str, str] | None:
    cfg = spec.get("credential")
    if not cfg:
        return None
    path = Path(str(cfg["file"]))
    data = path.read_bytes()
    got = hashlib.sha256(data).hexdigest()
    want = str(cfg["sha256"]).lower()
    if got != want:
        raise GatewayError(f"credential fingerprint mismatch for {path.name}")
    try:
        value = data.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise GatewayError(f"credential {path.name} is not UTF-8 text") from exc
    if not value or any(c in value for c in "\r\n"):
        raise GatewayError(f"credential {path.name} is empty or malformed")
    header = str(cfg.get("header") or "Authorization")
    prefix = str(cfg.get("prefix") or "Bearer ")
    return header, prefix + value


def _validate_args(spec: dict[str, Any], args: Any) -> list[str]:
    if not isinstance(args, dict):
        return ["args must be an object"]
    schema = spec.get("args_schema") or {}
    optional = set(spec.get("optional_args") or [])
    errors: list[str] = []
    for key, typ in schema.items():
        if key not in args:
            if key not in optional:
                errors.append(f"missing argument {key}")
            continue
        value = args[key]
        if isinstance(value, bool) and typ != "bool":
            errors.append(f"argument {key} must be {typ}")
        elif not isinstance(value, _TYPES[typ]):
            errors.append(f"argument {key} must be {typ}")
    for key in args:
        if key not in schema:
            errors.append(f"unexpected argument {key}")
    bad = control_plane_paths(args)
    if bad:
        errors.append("control-plane arguments are forbidden")
    return errors


class Gateway:
    def __init__(self, config_dir: str | Path | None = None) -> None:
        self.config = FarmConfig.load(config_dir)
        self.specs = validate_adapter_specs(self.config.adapters)
        self.fingerprint = self.config.safety_fingerprint()
        self.adapter_hash = adapter_config_hash(self.config.adapters)
        safety = self.config.farm.get("safety") or {}
        configured_model = str(safety.get("model_upstream") or "").rstrip("/")
        self.model_upstream = str(os.environ.get("QWENOMATIC_MODEL_UPSTREAM") or configured_model).rstrip("/")
        if not configured_model or self.model_upstream != configured_model:
            raise GatewayError("model upstream differs from the sealed safety configuration")
        parsed = urllib.parse.urlsplit(self.model_upstream)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise GatewayError("invalid fixed model upstream")

        secret_file = os.environ.get("QWENOMATIC_CAPABILITY_SECRET_FILE")
        if not secret_file:
            raise GatewayError("QWENOMATIC_CAPABILITY_SECRET_FILE is required")
        secret = Path(secret_file).read_bytes()
        if len(secret) < 32:
            raise GatewayError("capability secret is missing or too short")
        self.authority = TokenAuthority(secret)
        for spec in self.specs.values():
            _read_credential(spec)
        self.opener = urllib.request.build_opener(NoRedirect)

    def health(self) -> dict[str, Any]:
        return {
            "ok": True,
            "mode": "sealed",
            "safety_fingerprint": self.fingerprint,
            "adapter_config_hash": self.adapter_hash,
            "model_upstream_hash": hashlib.sha256(self.model_upstream.encode()).hexdigest(),
            "credentials_ok": True,
            "adapter_ids": sorted(self.specs),
        }

    def verify_capability(self, headers: Any, body: dict[str, Any], tool: str) -> None:
        auth = str(headers.get("Authorization") or "")
        if not auth.startswith("Capability "):
            raise GatewayError("missing capability")
        token = auth[len("Capability "):]
        try:
            claims = self.authority.verify(
                token,
                current_epoch=int(body["capability_epoch"]),
                generation_id=int(body["generation_id"]),
                policy_fingerprint=self.fingerprint,
            )
        except (InvalidToken, KeyError, TypeError, ValueError) as exc:
            raise GatewayError("invalid capability") from exc
        if claims.agent_id != body.get("agent_id") or tool not in claims.capabilities:
            raise GatewayError("capability scope mismatch")
        if body.get("policy_fingerprint") != self.fingerprint:
            raise GatewayError("safety policy mismatch")

    def invoke_adapter(self, adapter_id: str, headers: Any, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        spec = self.specs.get(adapter_id)
        if spec is None or body.get("adapter_id") != adapter_id:
            return 404, {"ok": False, "state": "rejected", "error": "unknown adapter"}
        tool = str(spec["tool"])
        if body.get("tool") != tool:
            return 403, {"ok": False, "state": "rejected", "error": "adapter/tool mismatch"}
        try:
            self.verify_capability(headers, body, tool)
        except GatewayError as exc:
            return 403, {"ok": False, "state": "rejected", "error": str(exc)}

        args = body.get("args")
        errors = _validate_args(spec, args)
        if errors:
            return 400, {"ok": False, "state": "rejected", "error": "; ".join(errors)}
        assert isinstance(args, dict)

        outgoing = {**dict(spec.get("fixed_body") or {}), **args}
        if str(spec.get("kind") or "http") == "payment":
            payment = spec["payment"]
            amount_field = str(payment.get("amount_field") or "amount")
            amount = float(args[amount_field])
            if amount <= 0 or amount > float(payment["hard_cap_per_action"]):
                return 403, {"ok": False, "state": "rejected", "error": "payment hard cap"}
            if amount > float(payment.get("material_threshold") or 0) and not body.get("approval_id"):
                return 403, {"ok": False, "state": "rejected", "error": "material payment lacks human approval"}
            outgoing["payee"] = str(payment["payee"])
            outgoing["currency"] = str(payment["currency"])

        destination = spec["destination"]
        request_headers = {"Content-Type": "application/json", "User-Agent": "qwenomatic-safety-gateway"}
        credential = _read_credential(spec)
        if credential:
            request_headers[credential[0]] = credential[1]
        req = urllib.request.Request(
            str(destination["url"]),
            data=json.dumps(outgoing, separators=(",", ":")).encode(),
            headers=request_headers,
            method=str(destination.get("method") or "POST").upper(),
        )
        invocation_id = str(body.get("invocation_id") or "")
        try:
            with self.opener.open(req, timeout=float(destination.get("timeout_seconds") or 30)) as response:
                raw = response.read(1 << 20)
                parsed = json.loads(raw or b"{}")
                if not isinstance(parsed, dict):
                    parsed = {}
        except urllib.error.HTTPError as exc:
            state = "uncertain" if exc.code >= 500 else "rejected"
            return (502 if state == "uncertain" else 400), {
                "ok": False, "state": state, "reference": invocation_id,
                "error": f"approved upstream returned HTTP {exc.code}",
            }
        except (OSError, TimeoutError, ValueError):
            return 502, {
                "ok": False, "state": "uncertain", "reference": invocation_id,
                "error": "approved upstream transport failure",
            }

        fields = spec.get("response_fields") or []
        result = {k: parsed.get(k) for k in fields if isinstance(k, str) and k in parsed}
        ref_field = spec.get("reference_field")
        reference = str(parsed.get(ref_field) if ref_field else invocation_id)
        return 200, {"ok": True, "state": "confirmed", "reference": reference[:200], "result": result}

    def proxy_model(self, method: str, suffix: str, body: bytes | None = None) -> tuple[int, bytes, str]:
        if (method, suffix) not in {("GET", "/models"), ("POST", "/chat/completions")}:
            return 404, b'{"error":"model path not allowed"}', "application/json"
        headers = {"User-Agent": "qwenomatic-safety-gateway"}
        if method == "POST":
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            self.model_upstream + suffix, data=body, headers=headers, method=method
        )
        try:
            with self.opener.open(req, timeout=180) as response:
                return response.status, response.read(8 << 20), response.headers.get("Content-Type", "application/json")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(1 << 20), "application/json"
        except OSError:
            return 502, b'{"error":"local model unavailable"}', "application/json"


def make_handler(gateway: Gateway):
    class Handler(BaseHTTPRequestHandler):
        server_version = "QwenomaticSafetyGateway/1"

        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def _json(self, code: int, data: dict[str, Any]) -> None:
            raw = json.dumps(data, separators=(",", ":")).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _body(self) -> bytes:
            try:
                n = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                n = 0
            if n < 0 or n > 1 << 20:
                raise GatewayError("request too large")
            return self.rfile.read(n)

        def do_GET(self) -> None:
            if self.path == "/healthz":
                return self._json(200, gateway.health())
            if self.path == "/model/v1/models":
                code, raw, ctype = gateway.proxy_model("GET", "/models")
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            self._json(404, {"error": "not found"})

        def do_POST(self) -> None:
            try:
                raw = self._body()
            except GatewayError as exc:
                return self._json(413, {"error": str(exc)})
            if self.path == "/model/v1/chat/completions":
                code, out, ctype = gateway.proxy_model("POST", "/chat/completions", raw)
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)
                return
            match = re.fullmatch(r"/adapter/([a-z][a-z0-9_.-]{0,63})/invoke", self.path)
            if not match:
                return self._json(404, {"error": "not found"})
            try:
                body = json.loads(raw or b"{}")
            except ValueError:
                return self._json(400, {"ok": False, "state": "rejected", "error": "invalid JSON"})
            if not isinstance(body, dict):
                return self._json(400, {"ok": False, "state": "rejected", "error": "invalid request"})
            code, out = gateway.invoke_adapter(match.group(1), self.headers, body)
            self._json(code, out)

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="qwenomatic-safety-gateway")
    parser.add_argument("--config-dir", default=os.environ.get("QWENOMATIC_CONFIG_DIR"))
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=int(os.environ.get("QWENOMATIC_SAFETY_GATEWAY_PORT", "8787")))
    args = parser.parse_args(argv)
    try:
        gateway = Gateway(args.config_dir)
    except (GatewayError, AdapterConfigError, OSError, ValueError) as exc:
        print(f"safety gateway refused to start: {exc}", file=sys.stderr)
        return 2
    server = ThreadingHTTPServer((args.host, args.port), make_handler(gateway))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
