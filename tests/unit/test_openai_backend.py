"""The real-model path, against a local fake of an OpenAI-compatible server.

This proves the wire format, prompt -> JSON -> gated tool call path, and
usage accounting. It does not prove anything about a real model's behaviour.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from helpers import make_supervisor, run_ticks
from runtime.inference import BackendUnavailable, InferenceRequest, OpenAICompatibleBackend
from storage.events import EventType


class FakeQwen(BaseHTTPRequestHandler):
    requests: list = []

    def log_message(self, *a):
        pass

    def _json(self, body, status=200):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._json({"data": [{"id": "Qwen3-14B-Instruct"}]})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeQwen.requests.append(body)
        system = body["messages"][0]["content"]
        segment = system.split("Target segment: ")[1].split("\n")[0]
        content = ("<think>The survey suggests a moderate price.</think>\n"
                   + json.dumps({"thought": "offer", "memory": "tried 19",
                                 "actions": [{"tool": "market.offer", "args": {"segment": segment, "price": 19}}]}))
        self._json({"choices": [{"message": {"role": "assistant", "content": content}}],
                    "usage": {"prompt_tokens": 480, "completion_tokens": 64}})


@pytest.fixture
def server():
    FakeQwen.requests = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeQwen)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/v1"
    srv.shutdown()


def test_client_wire_format(server):
    b = OpenAICompatibleBackend({"base_url": server, "model": "Qwen3-14B-Instruct", "quantization": "Q4_K_M",
                                 "max_concurrency": 2})
    assert b.health().ok
    gen = b.generate(InferenceRequest(messages=[{"role": "system", "content": "Target segment: s\n"}],
                                      max_tokens=100, temperature=0.2))
    assert gen.prompt_tokens == 480 and gen.completion_tokens == 64 and gen.gpu_seconds > 0
    sent = FakeQwen.requests[-1]
    assert sent["response_format"] == {"type": "json_object"} and sent["max_tokens"] == 100


def test_unreachable_server_is_unavailable():
    b = OpenAICompatibleBackend({"base_url": "http://127.0.0.1:9/v1", "model": "m", "timeout_seconds": 1})
    assert not b.health().ok
    with pytest.raises(BackendUnavailable):
        b.generate(InferenceRequest(messages=[]))


def test_farm_runs_on_an_openai_compatible_server(tmp_path, server):
    backend = OpenAICompatibleBackend({"base_url": server, "model": "Qwen3-14B-Instruct",
                                       "quantization": "Q4_K_M", "max_concurrency": 4})
    sup = make_supervisor(tmp_path, backend=backend)
    run_ticks(sup, 3)
    steps = sup.store.iter_events(types=[EventType.AGENT_STEP_COMPLETED])
    assert len(steps) == 3 * sup.config.farm["inference"]["slots_per_tick"]
    assert not any(e.payload["malformed"] for e in steps)
    assert len(sup.store.iter_events(types=[EventType.OPPORTUNITY])) == len(steps)
    usage = sup.store.iter_events(types=[EventType.INFERENCE_JOB_COMPLETED])[0].payload["usage"]
    assert usage["model"] == "Qwen3-14B-Instruct" and usage["quantization"] == "Q4_K_M"
    assert usage["prompt_tokens"] == 480
    # The prompt carries strategy and tools, never credentials or supervisor internals.
    prompt = json.dumps(FakeQwen.requests[-1]["messages"])
    assert "Tools:" in prompt and "market.offer" in prompt
    for secret in ("supervisor.key", "fitness.yaml", "risk_aversion", sup.token_for(steps[0].agent_id, 0)):
        assert secret not in prompt
