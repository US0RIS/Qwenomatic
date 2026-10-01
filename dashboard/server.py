"""Read-only dashboard server (standard library only).

Opens the ledger read-only, so it can run beside a live supervisor and can
never write to the farm.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from storage.events import EventStore

from .metrics import LedgerView

STATIC = Path(__file__).resolve().parent / "static"


def make_handler(view: LedgerView, lock: threading.Lock):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:  # quiet
            pass

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, data: Any) -> None:
            self._send(200, json.dumps(data, default=str).encode(), "application/json")

        def do_GET(self) -> None:  # noqa: N802
            url = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            if url.path == "/favicon.ico":
                self._send(204, b"", "image/x-icon")
                return
            if url.path in ("/", "/index.html"):
                self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
                return
            routes = {
                "/api/overview": lambda: view.overview(),
                "/api/population": lambda: view.population(),
                "/api/scheduler": lambda: view.scheduler(),
                "/api/evolution": lambda: view.evolution(),
                "/api/policy": lambda: view.policy(),
                "/api/audit": lambda: view.audit(
                    type=q.get("type") or None, agent_id=q.get("agent") or None,
                    before=int(q["before"]) if q.get("before") else None, limit=min(int(q.get("limit", 200)), 1000)),
            }
            fn = routes.get(url.path)
            if fn is None:
                self._send(404, b'{"error":"not found"}', "application/json")
                return
            try:
                with lock:
                    view.refresh()
                    data = fn()
            except ValueError as exc:
                self._send(400, json.dumps({"error": str(exc)}).encode(), "application/json")
                return
            self._json(data)

    return Handler


def serve(db_path: str | Path, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    store = EventStore(db_path, read_only=True)
    view = LedgerView(store)
    server = ThreadingHTTPServer((host, port), make_handler(view, threading.Lock()))
    return server
