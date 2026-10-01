"""Named-route egress broker for the network-less farm container.

The broker is a separate trusted process/container. It has network access;
the farm does not. Requests name a pre-approved route. There is deliberately
no CONNECT method, arbitrary URL parameter, or dynamic destination field.
"""

from __future__ import annotations

import argparse
import http.client
import http.server
import json
import os
import socketserver
import ssl
from pathlib import Path
from urllib.parse import urlsplit

from storage.events.canonical import digest


HOP_BY_HOP = {
    "connection", "proxy-connection", "keep-alive", "transfer-encoding",
    "te", "trailer", "upgrade", "proxy-authenticate", "proxy-authorization",
}
MAX_BODY = 16 * 1024 * 1024


class BrokerError(RuntimeError):
    pass


def validate_manifest(manifest: dict) -> dict:
    """Reject a manifest whose claimed digest does not match its route content."""
    destinations = manifest.get("destinations")
    claimed = str(manifest.get("manifest_digest", ""))
    if not isinstance(destinations, dict) or not claimed:
        raise BrokerError("invalid egress manifest")
    core = {"version": int(manifest.get("version", 1)), "destinations": destinations}
    actual = digest(core)
    if actual != claimed:
        raise BrokerError("egress manifest digest does not match route content")
    return {**core, "manifest_digest": actual}


class ThreadingUnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, socket_path: str, handler, manifest: dict):
        self.manifest = manifest
        super().__init__(socket_path, handler)


class Handler(http.server.BaseHTTPRequestHandler):
    server: ThreadingUnixHTTPServer
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        print(f"broker: {self.address_string()} - {fmt % args}")

    def do_CONNECT(self) -> None:
        self._json(405, {"error": "CONNECT is disabled; use a named approved route"})

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def do_PUT(self) -> None:
        self._handle()

    def do_PATCH(self) -> None:
        self._handle()

    def do_DELETE(self) -> None:
        self._handle()

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _handle(self) -> None:
        if self.path == "/__health":
            self._json(200, {
                "ok": True,
                "manifest_digest": self.server.manifest.get("manifest_digest", ""),
                "routes": sorted((self.server.manifest.get("destinations") or {}).keys()),
            })
            return

        parsed = urlsplit(self.path)
        parts = parsed.path.split("/")
        if len(parts) < 4 or parts[1] != "route":
            self._json(404, {"error": "named route required"})
            return
        route_id = parts[2]
        routes = self.server.manifest.get("destinations") or {}
        route = routes.get(route_id)
        if not isinstance(route, dict):
            self._json(403, {"error": "route is not operator-approved"})
            return

        length = int(self.headers.get("Content-Length", "0") or "0")
        if length < 0 or length > MAX_BODY:
            self._json(413, {"error": "request body too large"})
            return
        body = self.rfile.read(length) if length else None

        scheme = str(route.get("scheme", "https"))
        host = str(route["host"])
        port = int(route.get("port", 443 if scheme == "https" else 80))
        base_path = str(route.get("base_path", "")).rstrip("/")
        remainder = "/" + "/".join(parts[3:])
        upstream_path = (base_path + remainder) or "/"
        if parsed.query:
            upstream_path += "?" + parsed.query

        headers = {
            k: v for k, v in self.headers.items()
            if k.lower() not in HOP_BY_HOP
            and k.lower() not in {"host", "authorization", "cookie", "content-length"}
            and not k.lower().startswith("x-qwenomatic-")
        }
        headers["Host"] = host
        if body is not None:
            headers["Content-Length"] = str(len(body))

        credential = route.get("credential")
        if credential:
            env_var = str(credential.get("env_var", ""))
            secret = os.environ.get(env_var)
            if not secret:
                self._json(503, {"error": "approved route credential is unavailable to broker"})
                return
            header = str(credential.get("header", "Authorization"))
            prefix = str(credential.get("prefix", ""))
            headers[header] = prefix + secret

        conn = None
        try:
            if scheme == "https":
                conn = http.client.HTTPSConnection(
                    host, port, timeout=30, context=ssl.create_default_context()
                )
            elif scheme == "http":
                conn = http.client.HTTPConnection(host, port, timeout=30)
            else:
                raise BrokerError(f"unsupported route scheme {scheme!r}")
            conn.request(self.command, upstream_path, body=body, headers=headers)
            resp = conn.getresponse()
            data = resp.read(MAX_BODY + 1)
            if len(data) > MAX_BODY:
                raise BrokerError("upstream response too large")
        except Exception as exc:
            self._json(502, {"error": f"approved route failed: {type(exc).__name__}"})
            return
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

        self.send_response(resp.status)
        for k, v in resp.getheaders():
            lk = k.lower()
            if lk in HOP_BY_HOP or lk in {
                "set-cookie", "www-authenticate", "proxy-authenticate", "content-length"
            }:
                continue
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="qwenomatic-egress-broker")
    p.add_argument("--manifest", required=True)
    p.add_argument("--socket", default="/run/qwenomatic/egress.sock")
    args = p.parse_args(argv)
    try:
        manifest = validate_manifest(json.loads(Path(args.manifest).read_text("utf-8")))
    except (OSError, json.JSONDecodeError, BrokerError, ValueError) as exc:
        raise SystemExit(f"invalid egress manifest: {exc}") from exc
    socket_path = Path(args.socket)
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        socket_path.unlink()
    except FileNotFoundError:
        pass
    server = ThreadingUnixHTTPServer(str(socket_path), Handler, manifest)
    os.chmod(socket_path, 0o660)
    print(
        f"egress broker listening on unix://{socket_path}; "
        f"routes={sorted(manifest['destinations'])}"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        try:
            socket_path.unlink()
        except FileNotFoundError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
