"""Shared inference service.

Many logical agents, one (or few) model servers: agents are state + policy,
not separate model copies. Jobs are queued by scheduler priority and executed
up to the backend's concurrency.

Required interface (DESIGN §6):
    submit(agent_id, request, priority, budget) -> job_id
    cancel(job_id)
    checkpoint(job_id)
    usage(job_id) -> Usage
    health() -> Health
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable

from .base import BackendUnavailable, Generation, Health, InferenceBackend, InferenceRequest, Usage


@dataclass
class Job:
    job_id: str
    agent_id: str
    request: InferenceRequest
    priority: float
    budget: dict[str, Any]
    seq: int
    state: str = "queued"  # queued | running | done | failed | cancelled
    text: str | None = None
    error: str | None = None
    usage: Usage | None = None
    submitted_wall: float = 0.0


class InferenceService:
    def __init__(self, backend: InferenceBackend, *, escalation: InferenceBackend | None = None,
                 escalation_allowed: Callable[[], bool] | None = None, new_id: Callable[[], str] | None = None) -> None:
        self.backend = backend
        self.escalation = escalation
        self.escalation_allowed = escalation_allowed or (lambda: False)
        self._new_id = new_id
        self._jobs: dict[str, Job] = {}
        self._seq = 0
        self._lock = threading.Lock()

    # -------------------------------------------------------- required API
    def submit(self, agent_id: str, request: InferenceRequest, priority: float, budget: dict[str, Any],
               job_id: str | None = None) -> str:
        max_tokens = int(budget.get("max_tokens", request.max_tokens))
        request.max_tokens = max(1, min(request.max_tokens, max_tokens))
        with self._lock:
            self._seq += 1
            job_id = job_id or (self._new_id() if self._new_id else f"job-{self._seq}")
            self._jobs[job_id] = Job(job_id, agent_id, request, priority, budget, self._seq,
                                     submitted_wall=time.perf_counter())
        return job_id

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if job and job.state in ("queued", "running"):
                job.state = "cancelled"
                return True
        return False

    def checkpoint(self, job_id: str) -> dict[str, Any]:
        """Capture enough to resume: jobs are stateless requests, so the request is the checkpoint."""
        job = self._jobs.get(job_id)
        if job is None:
            return {}
        return {"job_id": job.job_id, "agent_id": job.agent_id, "state": job.state, "priority": job.priority,
                "max_tokens": job.request.max_tokens, "messages": len(job.request.messages)}

    def usage(self, job_id: str) -> Usage | None:
        job = self._jobs.get(job_id)
        return job.usage if job else None

    def health(self) -> Health:
        try:
            return self.backend.health()
        except Exception as exc:  # pragma: no cover - defensive
            return Health(False, self.backend.name, self.backend.model, repr(exc))

    # ------------------------------------------------------------ execution
    def run_queued(self) -> list[Job]:
        """Run every queued job (highest priority first). Returns jobs in submission order."""
        with self._lock:
            queued = sorted((j for j in self._jobs.values() if j.state == "queued"),
                            key=lambda j: (-j.priority, j.seq))
            for j in queued:
                j.state = "running"
        if not queued:
            return []
        workers = max(1, min(self.backend.max_concurrency, len(queued)))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="inference") as pool:
            list(pool.map(self._run_one, queued))
        with self._lock:
            finished = sorted(queued, key=lambda j: j.seq)
            for j in finished:
                self._jobs.pop(j.job_id, None)
        return finished

    def _run_one(self, job: Job) -> None:
        if job.state == "cancelled":
            return
        started = time.perf_counter()
        backend = self.backend
        try:
            try:
                gen: Generation = backend.generate(job.request)
            except BackendUnavailable:
                if self.escalation is None or not self.escalation_allowed():
                    raise
                backend = self.escalation
                gen = backend.generate(job.request)
        except Exception as exc:
            job.state = "failed"
            job.error = f"{type(exc).__name__}: {exc}"[:500]
            return
        if job.state == "cancelled":
            return
        queue_latency = 0.0 if getattr(backend, "simulated", False) else max(0.0, started - job.submitted_wall)
        job.text = gen.text
        job.usage = Usage(
            model=backend.model, quantization=backend.quantization, backend=backend.name, owner=job.agent_id,
            prompt_tokens=gen.prompt_tokens, completion_tokens=gen.completion_tokens,
            wall_seconds=round(gen.wall_seconds, 6), gpu_seconds=round(gen.gpu_seconds, 6),
            queue_latency_seconds=round(queue_latency, 6), cloud_usd=round(gen.cloud_usd, 6),
        )
        job.state = "done"
