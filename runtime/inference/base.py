"""Inference backend interface (DESIGN §6)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Any


class BackendUnavailable(Exception):
    pass


@dataclass
class InferenceRequest:
    messages: list[dict[str, str]]
    max_tokens: int = 512
    temperature: float = 0.7
    # Whether a reasoning model should think before answering. None leaves the
    # server's default; True/False is the supervisor's per-step decision.
    thinking: bool | None = None
    # Opaque to real models; the simulated backend reads the agent's genotype
    # and memory from here instead of parsing prose.
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Generation:
    text: str
    prompt_tokens: int
    completion_tokens: int
    wall_seconds: float
    gpu_seconds: float
    cloud_usd: float = 0.0


@dataclass
class Usage:
    model: str
    quantization: str
    backend: str
    owner: str
    prompt_tokens: int
    completion_tokens: int
    wall_seconds: float
    gpu_seconds: float
    queue_latency_seconds: float
    cloud_usd: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Health:
    ok: bool
    backend: str
    model: str
    detail: str = ""


class InferenceBackend(ABC):
    name: str = "backend"
    model: str = "unknown"
    quantization: str = "unknown"
    max_concurrency: int = 1
    local: bool = True

    @abstractmethod
    def generate(self, request: InferenceRequest) -> Generation: ...

    @abstractmethod
    def health(self) -> Health: ...
