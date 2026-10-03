"""The real-model path, against a local fake of an OpenAI-compatible server.

This proves the wire format, prompt -> JSON -> gated tool call path, and
usage accounting. It does not prove anything about a real model's behaviour.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from helpers import make_supervisor, run_ticks
from broker.inference import InferenceProxy
from runtime.inference import BackendUnavailable, InferenceRequest, OpenAICompatibleBackend
from storage.events import EventType, digest


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
        context = json.loads(body['messages'][1]['content'])
        segment = context['strategy']['target']['segment']
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
    gen = b.generate(InferenceRequest(messages=[{"role": "system", "content": "Tool catalog"},
                                                {"role": "user", "content": json.dumps({"strategy":{"target":{"segment":"s"}}})}],
                                      max_tokens=100, temperature=0.2, metadata={"seed": 424242}))
    assert gen.prompt_tokens == 480 and gen.completion_tokens == 64 and gen.gpu_seconds > 0
    sent = FakeQwen.requests[-1]
    assert sent["response_format"] == {"type": "json_object"} and sent["max_tokens"] == 100
    assert sent["seed"] == 424242


def test_unreachable_server_is_unavailable():
    b = OpenAICompatibleBackend({"base_url": "http://127.0.0.1:9/v1", "model": "m", "timeout_seconds": 1})
    assert not b.health().ok
    with pytest.raises(BackendUnavailable):
        b.generate(InferenceRequest(messages=[]))


def test_initial_population_seeds_fit_fixed_broker_scope(tmp_path):
    """Business and specialist jobs from the shipped 17/2/1 population must all reach Qwen."""
    sup = make_supervisor(tmp_path, {"farm": {"roles": {"enabled": True}}})
    proxy = InferenceProxy({
        "endpoint": "http://10.204.2.2:11434/v1/chat/completions", "model": "fixed",
        "max_tokens": 4096, "max_messages": 32, "max_content_bytes": 65536,
        "requests_per_period": 10000, "period_seconds": 3600, "concurrency": 4,
    })
    try:
        population = list(sup.state.agents.values())
        assert len(population) == 20
        assert {role: sum(a.role == role for a in population) for role in ("business", "research", "red_team")} == {
            "business": 17, "research": 2, "red_team": 1}
        high_raw_seeds = 0
        for agent in population:
            _, state, _ = sup.runtime.load_state(agent.id)
            if agent.role == "business":
                request = sup.runtime.build_request(agent, state, sup.config.seed)
                raw_seed = int(digest([sup.config.seed, agent.id, 0, "inference"])[:8], 16)
            else:
                request = sup.specialists.build_request(agent, state)
                raw_seed = int(digest([sup.config.seed, agent.id, 0])[:8], 16)
            high_raw_seeds += raw_seed > 0x7fffffff
            assert request.metadata["seed"] == raw_seed & 0x7fffffff
            proxy.validate({"model": "fixed", "messages": request.messages,
                            "max_tokens": request.max_tokens, "temperature": request.temperature,
                            "seed": request.metadata["seed"], "response_format": {"type": "json_object"}})
        assert high_raw_seeds > 0  # This exact configuration used to fail at tick zero.
    finally:
        sup.close()


def test_real_backend_outage_does_not_advance_simulated_economy(tmp_path):
    from supervisor.core import Supervisor
    from supervisor.config import FarmConfig
    from supervisor.safety.boundary import NetworkBoundary

    # The normal acceptance harness installs an empty test boundary. Only the
    # real backend's unavailable transport is under test here.
    cfg = FarmConfig.load(data_dir=tmp_path / "farm", overrides={"farm": {
        "inference": {"backend": "openai_compatible", "openai_compatible": {
            "base_url": "http://127.0.0.1:9/v1", "model": "missing",
            "timeout_seconds": 1}}}})
    sup = Supervisor(cfg)
    try:
        sup.bootstrap()
        before = sup.clock.tick
        with pytest.raises(BackendUnavailable, match="stopped without advancing simulated time"):
            sup.tick()
        assert sup.clock.tick == before
        assert not sup.store.iter_events(types=[EventType.FINANCIAL_EVENT, EventType.SCHEDULER_ALLOCATION])
        assert any(e.payload.get("ok") is False for e in sup.store.iter_events(types=[EventType.HEALTH_EVENT]))
    finally:
        sup.close()


def test_farm_runs_on_an_openai_compatible_server(tmp_path, server):
    backend = OpenAICompatibleBackend({"base_url": server, "model": "Qwen3-14B-Instruct",
                                       "quantization": "Q4_K_M", "max_concurrency": 4})
    sup = make_supervisor(tmp_path, {"farm": {"inference": {"backend": "openai_compatible",
                             "openai_compatible": {"base_url": server, "model": "Qwen3-14B-Instruct",
                                                   "quantization": "Q4_K_M", "max_concurrency": 4}}}})
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
    assert "Tool catalog" in prompt and "market.offer" in prompt
    for secret in ("supervisor.key", "fitness.yaml", "risk_aversion", sup.token_for(steps[0].agent_id, 0)):
        assert secret not in prompt
