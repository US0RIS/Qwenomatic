"""Client for an OpenAI-compatible local model server.

Works with llama.cpp `llama-server`, vLLM and Ollama serving a Qwen model on
the local GPU. Standard library only.
"""

from __future__ import annotations

import json
import time
import ssl
import urllib.error
import urllib.request
from typing import Any

from .base import BackendUnavailable, Generation, Health, InferenceBackend, InferenceRequest


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "model redirects are prohibited", headers, fp)


class OpenAICompatibleBackend(InferenceBackend):
    name = "openai_compatible"

    def __init__(self, config: dict[str, Any]) -> None:
        handlers = [urllib.request.ProxyHandler({}), NoRedirect()]
        self.broker_transport = config.get("broker_transport")
        if self.broker_transport:
            c = self.broker_transport
            context = ssl.create_default_context(cafile=c["ca_file"])
            context.minimum_version = ssl.TLSVersion.TLSv1_3
            context.load_cert_chain(c["client_cert"], c["client_key"])
            handlers.append(urllib.request.HTTPSHandler(context=context))
        self._opener = urllib.request.build_opener(*handlers)
        self.base_url = config["base_url"].rstrip("/")
        self.model = config["model"]
        self.quantization = config.get("quantization", "unknown")
        self.max_concurrency = int(config.get("max_concurrency", 1))
        self.timeout = float(config.get("timeout_seconds", 120))
        self.json_mode = bool(config.get("json_mode", True))
        self.api_key = config.get("api_key")
        self.cost_per_1k_tokens = float(config.get("cost_per_1k_tokens", 0.0))
        self.local = bool(config.get("local", True))

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(f"{self.base_url}{path}", data=json.dumps(body).encode(), headers=headers)
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                return json.loads(resp.read())
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            raise BackendUnavailable(str(exc)) from exc

    def generate(self, request: InferenceRequest) -> Generation:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": request.messages,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
        }
        if self.json_mode:
            body["response_format"] = {"type": "json_object"}
        if "seed" in request.metadata:
            body["seed"] = int(request.metadata["seed"])
        started = time.perf_counter()
        data = self._post("/chat/completions", body)
        wall = time.perf_counter() - started
        text = data["choices"][0]["message"].get("content") or ""
        usage = data.get("usage", {})
        prompt_tokens = int(usage.get("prompt_tokens", 0))
        completion_tokens = int(usage.get("completion_tokens", 0))
        # One GPU shared by `max_concurrency` parallel slots: attribute the
        # job an equal share of the wall time it occupied.
        gpu = wall / max(1, self.max_concurrency) if self.local else 0.0
        cloud = 0.0 if self.local else (prompt_tokens + completion_tokens) / 1000 * self.cost_per_1k_tokens
        return Generation(text, prompt_tokens, completion_tokens, wall_seconds=wall, gpu_seconds=gpu, cloud_usd=cloud)

    def health(self) -> Health:
        if self.broker_transport:
            try:
                self._post("/chat/completions", {"model": self.model, "messages": [{"role": "user", "content": "Reply OK"}], "max_tokens": 1, "temperature": 0})
                return Health(True, self.name, self.model, "broker chat verified")
            except urllib.error.HTTPError as exc:
                return Health(False, self.name, self.model,
                              f"broker chat rejected (HTTP {exc.code}); inspect the broker terminal")
            except Exception as exc:
                # Do not log response bodies, prompts or certificate material.
                return Health(False, self.name, self.model,
                              f"broker chat unavailable ({type(exc).__name__}); inspect the broker terminal")
        req = urllib.request.Request(f"{self.base_url}/models")
        try:
            with self._opener.open(req, timeout=5) as resp:
                ok = resp.status == 200
        except Exception as exc:
            return Health(False, self.name, self.model, str(exc))
        return Health(ok, self.name, self.model, "ok" if ok else "unhealthy")
