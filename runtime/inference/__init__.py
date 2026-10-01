"""Shared inference: many logical agents over one local model server."""

from typing import Any

from .base import BackendUnavailable, Generation, Health, InferenceBackend, InferenceRequest, Usage
from .openai_compat import OpenAICompatibleBackend
from .service import InferenceService, Job
from .simulated import SimulatedBackend


def build_backend(config: dict[str, Any], seed: int) -> InferenceBackend:
    kind = config.get("backend", "simulated")
    if kind == "simulated":
        return SimulatedBackend(config.get("simulated", {}), seed)
    if kind == "openai_compatible":
        return OpenAICompatibleBackend(config["openai_compatible"])
    raise ValueError(f"unknown inference backend {kind!r}")


__all__ = [
    "BackendUnavailable", "Generation", "Health", "InferenceBackend", "InferenceRequest", "InferenceService", "Job",
    "OpenAICompatibleBackend", "SimulatedBackend", "Usage", "build_backend",
]
