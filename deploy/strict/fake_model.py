"""Tiny OpenAI-compatible fake used only by the strict-network CI job."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def _send(self, data, code=200):
        raw = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path == "/v1/models":
            self._send({"object": "list", "data": [{"id": "qwen3:14b", "object": "model"}]})
        else:
            self._send({"error": "not found"}, 404)

    def do_POST(self):
        if self.path != "/v1/chat/completions":
            return self._send({"error": "not found"}, 404)
        n = int(self.headers.get("Content-Length") or 0)
        if n:
            self.rfile.read(n)
        self._send({
            "choices": [{"message": {"content": '{"thought":"ci","actions":[]}'}}],
            "usage": {"prompt_tokens": 8, "completion_tokens": 8},
        })


ThreadingHTTPServer(("0.0.0.0", 11434), Handler).serve_forever()
