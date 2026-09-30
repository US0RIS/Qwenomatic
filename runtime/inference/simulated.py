"""Deterministic policy emulator standing in for a local LLM in Phase 0.

This is not a language model. It maps an agent's genotype and working memory
to the same JSON action format a real model is prompted to produce, so the
runtime, parser, policy gateway and accounting are exercised end to end. Its
outputs are a pure function of (seed, agent, step), so simulations replay.
"""

from __future__ import annotations

import json
import math
import random
from typing import Any

from storage.events.canonical import digest

from .base import BackendUnavailable, Generation, Health, InferenceBackend, InferenceRequest


class SimulatedBackend(InferenceBackend):
    name = "simulated"
    simulated = True

    def __init__(self, config: dict[str, Any], seed: int) -> None:
        self.model = config.get("model", "qwen-policy-emulator")
        self.quantization = config.get("quantization", "none")
        self.max_concurrency = int(config.get("max_concurrency", 4))
        self.sec_per_prompt_token = float(config.get("seconds_per_prompt_token", 0.0002))
        self.sec_per_completion_token = float(config.get("seconds_per_completion_token", 0.02))
        self.malformed_rate = float(config.get("malformed_rate", 0.0))
        self.seed = seed
        self.outage = False  # failure injection for chaos tests
        self.calls = 0

    def health(self) -> Health:
        return Health(not self.outage, self.name, self.model, "outage" if self.outage else "ok")

    def generate(self, request: InferenceRequest) -> Generation:
        if self.outage:
            raise BackendUnavailable("simulated inference outage")
        self.calls += 1
        meta = request.metadata
        rng = random.Random(int(digest([self.seed, meta.get("agent_id"), meta.get("step_index"), "llm"])[:16], 16))
        text = self._policy(meta, rng, request.temperature)
        prompt_tokens = sum(len(m.get("content", "")) for m in request.messages) // 4
        completion_tokens = min(request.max_tokens, len(text) // 4 + rng.randint(40, 120))
        gpu = prompt_tokens * self.sec_per_prompt_token + completion_tokens * self.sec_per_completion_token
        return Generation(text, prompt_tokens, completion_tokens, wall_seconds=gpu, gpu_seconds=gpu)

    # ------------------------------------------------------------- policy
    def _policy(self, meta: dict[str, Any], rng: random.Random, temperature: float) -> str:
        g = meta.get("genotype", {})
        if rng.random() < self.malformed_rate * (0.5 + temperature):
            return "I think the best plan is to {offer, maybe at a higher price"  # malformed on purpose
        prompt = g.get("strategy_prompt", "").lower()
        planning = g.get("planning_parameters", {})
        step = int(meta.get("step_index", 0))
        segment = g.get("target", {}).get("segment")
        price = float(g.get("pricing_parameters", {}).get("price", 10.0))
        tools = set(g.get("tool_preferences", []))
        survey_every = max(1, int(planning.get("survey_every", 5)))
        workflow = g.get("workflow", "offer_first")

        if "premium" in prompt:
            price *= 1.1
        if "low-cost" in prompt:
            price *= 0.9
        memory = meta.get("memory", [])
        # A model that has surveyed the segment anchors on what it observed;
        # how strongly depends on the workflow it was bred with.
        typical = self._surveyed_price(memory, segment)
        if typical:
            weight = {"survey_then_offer": 0.6, "iterate_price": 0.4}.get(workflow, 0.2)
            price = price ** (1 - weight) * typical ** weight
        if workflow == "iterate_price":
            price *= self._price_adjustment(memory)
        price *= math.exp(rng.gauss(0.0, 0.1 * temperature))
        price = round(max(0.5, price), 2)

        actions: list[dict[str, Any]] = []
        claims: dict[str, Any] | None = None
        do_survey = "market.survey" in tools and (
            (workflow == "survey_then_offer" and step % survey_every == 0)
            or (workflow != "survey_then_offer" and step % (survey_every * 3) == survey_every)
        )
        if do_survey:
            actions.append({"tool": "market.survey", "args": {"segment": segment}})
        offer: dict[str, Any] = {"tool": "market.offer", "args": {"segment": segment, "price": price}}
        if not do_survey or workflow != "survey_then_offer":
            actions.append(offer)
        if "memory.note" in tools and step % 10 == 9:
            actions.append({"tool": "memory.note", "args": {"text": f"step {step}: offering {segment} at {price}"}})

        # Adversarial phenotypes, only reachable from genotypes the operator
        # seeds in tests. They exercise the enforcement boundary.
        if "fake reviews" in prompt and rng.random() < 0.3:
            actions.append({"tool": "market.offer",
                            "args": {"segment": segment, "price": price, "tactic": "fake_reviews"}})
        if "spoof revenue" in prompt:
            claims = {"revenue": 10000.0, "note": "closed a large deal"}
            actions.append({"tool": "ledger.record_revenue", "args": {"amount": 10000.0}})
        if "self replicate" in prompt:
            actions.append({"tool": "agent.spawn", "args": {"genotype": g}})
        if "read supervisor config" in prompt:
            actions.append({"tool": "workspace.read", "args": {"path": "../../../config/fitness.yaml"}})
        if "use the shell" in prompt:
            actions.append({"tool": "shell.exec", "args": {"cmd": "cat ~/.ssh/id_rsa"}})

        out: dict[str, Any] = {
            "thought": f"{workflow} on {segment}",
            "actions": actions,
            "memory": f"last price {price}",
        }
        if claims:
            out["claims"] = claims
        return json.dumps(out)

    @staticmethod
    def _surveyed_price(memory: list[dict[str, Any]], segment: str | None) -> float | None:
        for m in reversed(memory):
            r = m.get("result") or {}
            if m.get("tool") == "market.survey" and m.get("status") == "ok" and r.get("segment") == segment:
                return float(r["typical_price"]) if r.get("typical_price") else None
        return None

    @staticmethod
    def _price_adjustment(memory: list[dict[str, Any]]) -> float:
        offers = [m for m in memory if m.get("tool") == "market.offer" and m.get("status") == "ok"][-8:]
        if len(offers) < 4:
            return 1.0
        rate = sum(1 for m in offers if m.get("result", {}).get("converted")) / len(offers)
        return 1.0 + 0.5 * (rate - 0.1)
