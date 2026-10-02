"""The immutable supervisor (README "Architecture").

Owns lifecycle (I1), credentials (I2), money (I3), fitness (I4), policy (I5)
and reproduction (I6). Population agents are untrusted workloads whose only
channel to the world is the capability gateway.
"""

from __future__ import annotations

import json
import os
import random

try:
    import fcntl  # POSIX
except ImportError:  # pragma: no cover - exercised on Windows
    fcntl = None  # type: ignore[assignment]
    import msvcrt
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

from runtime.agent import GENOTYPE_VERSION, AgentRuntime, random_genotype, validate_genotype
from runtime.inference import InferenceBackend, InferenceService, build_backend
from runtime.tools.base import ToolRegistry
from runtime.tools.market import MarketOfferTool, MarketSurveyTool, MemoryNoteTool
from runtime.tools.sim_market import SimulatedMarket
from runtime.tools.workspace import WorkspaceReadTool, WorkspaceWriteTool
from storage.events import EventStore, EventType, FarmState, digest

from .accounting import (
    AdSpendMeter, Attribution, ComplianceMonitor, ComputeMeter, HumanLaborMeter, Ledger, MarketObserver,
    MilestoneValidator, PaymentProcessorAdapter, check_agent_budget, farm_gpu_last_day, farm_spend_last_day,
)
from .clock import SimulatedClock, WallClock, parse_ts
from .config import FarmConfig, config_hash
from .evolution import GenerationManager, MutationEngine
from .ids import IdFactory, stable_id
from .policy import PolicyEngine, StepContext, TokenAuthority, ToolGateway, load_or_create_secret
from .policy import approvals as approvals_mod
from .safety import SafetyBoundaryError, attest_network_boundary
from .scheduler import Candidate, Scheduler


class SupervisorLocked(Exception):
    pass


@dataclass
class TickResult:
    tick: int
    generation: int | None
    scheduled: int = 0
    completed: int = 0
    failed: int = 0
    closed_generation: int | None = None
    halted: bool = False
    notes: list[str] = field(default_factory=list)


class Supervisor:
    def __init__(
        self,
        config: FarmConfig,
        *,
        backend: InferenceBackend | None = None,
        market: SimulatedMarket | None = None,
        lock: bool = True,
    ) -> None:
        self.config = config
        self.data_dir = Path(config.data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._lock_fh = self._acquire_lock() if lock else None
        farm = config.farm

        clock_cfg = farm.get("clock", {"mode": "simulated"})
        if clock_cfg.get("mode", "simulated") == "simulated":
            self.clock: SimulatedClock | WallClock = SimulatedClock(clock_cfg["start"], config.tick_seconds)
            self.ids = IdFactory(seed=config.seed)
        else:
            self.clock = WallClock(config.tick_seconds)
            self.ids = IdFactory(seed=None)

        self.store = EventStore(self.data_dir / "ledger.sqlite3", now=self.clock.now,
                                new_id=lambda: self.ids.new("event"))
        self.state = FarmState(cap_window=max(256, int(farm["scheduler"].get("cap_window_ticks", 36)) * 2))
        self.state.on_rollback = self._rebuild_state  # type: ignore[method-assign]
        self._rebuild_state()
        self.store.subscribe(self.state)
        recovered = self.state.last_seq > 0
        try:
            self.safety_attestation = attest_network_boundary(config, self.store)
        except Exception as exc:
            try:
                self.store.append(
                    EventType.SAFETY_ATTESTATION_FAILED,
                    {"error": str(exc)[:500], "fingerprint": config.safety_fingerprint()},
                )
            finally:
                self.close()
            if isinstance(exc, SafetyBoundaryError):
                raise
            raise SafetyBoundaryError(f"safety attestation errored: {exc}") from exc
        sessions = len(self.store.iter_events(types=[EventType.SUPERVISOR_STARTED]))
        self.ids.start_session(sessions + 1)
        self.clock.set_tick(self.state.last_tick + 1)

        capability_secret = os.environ.get("QWENOMATIC_CAPABILITY_SECRET_FILE")
        secret_path = Path(capability_secret) if capability_secret else self.data_dir / "secrets" / "supervisor.key"
        self.authority = TokenAuthority(load_or_create_secret(secret_path))
        econ = farm["economy"]
        self.ledger = Ledger(self.store, farm_controlled_accounts=econ.get("farm_controlled_accounts", []),
                             currency=econ.get("currency", "USD"))
        self.payments = PaymentProcessorAdapter(self.ledger, self.state)
        self.ads = AdSpendMeter(self.ledger)
        self.compute = ComputeMeter(
            self.ledger, lambda: float(self.fitness_config()["imputed_costs"]["local_compute_usd_per_gpu_hour"]))
        self.labor = HumanLaborMeter(
            self.ledger, lambda: float(self.fitness_config()["imputed_costs"].get("human_intervention_usd", 0.0)))
        self.observer = MarketObserver(self.store)
        self.milestones = MilestoneValidator(self.store)
        self.compliance = ComplianceMonitor(self.store)
        for adapter in (self.payments, self.ads, self.compute, self.labor, self.observer, self.milestones,
                        self.compliance):
            self.ledger.register_adapter(adapter)

        start = self.clock.start if isinstance(self.clock, SimulatedClock) else parse_ts(clock_cfg.get("start", self.clock.now()))
        self.market = market or SimulatedMarket(farm["simulation"]["market"], config.seed, start)
        segments = list(econ["segments"])
        self.registry = ToolRegistry()
        self.registry.register(MarketOfferTool(self.market, self.observer, self.payments, self.ads, segments))
        self.registry.register(MarketSurveyTool(self.market, segments))
        self.registry.register(MemoryNoteTool())
        quota = int(farm["runtime"].get("workspace_quota_bytes", 262144))
        self.registry.register(WorkspaceWriteTool(quota))
        self.registry.register(WorkspaceReadTool())

        self._policy_cache: dict[int, PolicyEngine] = {}
        self.gateway = ToolGateway(
            store=self.store, state=self.state, registry=self.registry, authority=self.authority,
            policy=self.policy_engine, policy_fingerprint=self.current_policy_fingerprint,
            new_id=self.ids.new, farm_spend_day=self._farm_spend_day,
            on_hard_violation=self._on_hard_violation,
        )
        self.backend = backend or build_backend(farm["inference"], config.seed)
        esc = farm["inference"].get("escalation", {})
        self.inference = InferenceService(
            self.backend,
            escalation_allowed=lambda: bool(esc.get("enabled")) and self._cloud_spend_total() < float(esc.get("ceiling_usd", 0)),
            new_id=lambda: self.ids.new("job"),
        )
        self.runtime = AgentRuntime(store=self.store, registry=self.registry, gateway=self.gateway,
                                    config=farm["runtime"])
        self.mutations = MutationEngine(
            farm["mutation"], segments=segments, granted_tools=sorted(self.config.policy.get("capabilities", {})),
            max_mutations=int(farm["evolution"].get("max_mutations", 2)),
        )
        self.generations = GenerationManager(self)
        self.store.append(
            EventType.SUPERVISOR_STARTED,
            {"session": sessions + 1, "recovered": recovered, "pid": os.getpid(),
             "config_hashes": self.config.hashes(), "backend": self.backend.name, "model": self.backend.model},
        )
        self._last_backend_ok = True
        if recovered:
            self.recover()

    # ================================================================ setup
    def _acquire_lock(self):
        """Acquire the single-supervisor process lock on POSIX or Windows."""
        path = self.data_dir / "supervisor.lock"
        # Windows byte-range locking requires at least one byte to exist.
        path.touch(exist_ok=True)
        fh = open(path, "r+", encoding="utf-8")
        fh.seek(0, os.SEEK_END)
        if fh.tell() == 0:
            fh.write(" ")
            fh.flush()
        try:
            if fcntl is not None:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            fh.close()
            raise SupervisorLocked(f"another supervisor holds {path}") from exc

        # PID is diagnostic only; the OS lock is authoritative.
        fh.seek(0)
        fh.write(f"{os.getpid():<20}")
        fh.flush()
        return fh

    def close(self) -> None:
        self.store.close()
        if self._lock_fh:
            try:
                if fcntl is not None:
                    fcntl.flock(self._lock_fh, fcntl.LOCK_UN)
                else:
                    self._lock_fh.seek(0)
                    msvcrt.locking(self._lock_fh.fileno(), msvcrt.LK_UNLCK, 1)
            finally:
                self._lock_fh.close()
                self._lock_fh = None

    def _rebuild_state(self) -> None:
        self.state.reset()
        self.state.replay(self.store.iter_events())

    # ======================================================= configuration
    def generation_config(self, generation: int | None = None) -> dict[str, Any]:
        g = self.state.current_generation if generation is None else generation
        if g is None or g not in self.state.generations:
            return self._fresh_generation_config()
        return self.state.generations[g].config

    def _fresh_generation_config(self) -> dict[str, Any]:
        farm = self.config.farm
        cfg = {
            "fitness": self.config.fitness,
            "policy": self.config.policy,
            "scheduler": farm["scheduler"],
            "evolution": farm["evolution"],
        }
        cfg["timing"] = {"tick_seconds": self.config.tick_seconds,
                         "gpu_count": int(farm["inference"].get("gpu_count", 1))}
        cfg["hashes"] = {k: config_hash(v) for k, v in cfg.items()}
        cfg["hashes"]["farm"] = config_hash(farm)
        cfg["hashes"]["safety"] = self.config.safety_fingerprint()
        return cfg

    def fitness_config(self) -> dict[str, Any]:
        return self.generation_config()["fitness"]

    def policy_engine(self) -> PolicyEngine:
        g = self.state.current_generation if self.state.current_generation is not None else -1
        if g not in self._policy_cache:
            self._policy_cache[g] = PolicyEngine(self.generation_config()["policy"])
        return self._policy_cache[g]

    def current_policy_fingerprint(self) -> str:
        g = self.state.current_generation
        if g is not None and g in self.state.generations:
            return str(self.state.generations[g].config.get("hashes", {}).get("safety")
                       or self.config.safety_fingerprint())
        return self.config.safety_fingerprint()

    def scheduler_for(self, generation: int) -> Scheduler:
        cfg = self.generation_config(generation)
        return Scheduler(cfg["scheduler"], seed=self.config.seed,
                         min_exposure_steps=int(cfg["fitness"].get("min_exposure_steps", 0)),
                         variance_floor=float(cfg["fitness"].get("posterior", {}).get("variance_floor", 0.25)))

    @property
    def ticks_per_day(self) -> int:
        return max(1, round(86400 / self.config.tick_seconds))

    def _farm_spend_day(self) -> float:
        return farm_spend_last_day(self.state, self.clock.tick, self.ticks_per_day)

    def _cloud_spend_total(self) -> float:
        return sum(c.api_cloud_spend for gen in self.state.counters.values() for c in gen.values())

    # ============================================================ lifecycle
    def random_genotype(self, rng: random.Random) -> dict[str, Any]:
        return random_genotype(rng, self.config.farm["mutation"], list(self.config.segments))

    def create_agent(self, *, agent_id: str, lineage_id: str, parent_id: str | None, generation: int,
                     genotype: dict[str, Any], origin: str, mutations: list[dict[str, Any]] | None = None) -> None:
        errors = validate_genotype(genotype, mutation_cfg=self.config.farm["mutation"],
                                   segments=list(self.config.segments),
                                   granted_tools=sorted(self.config.policy.get("capabilities", {})))
        if errors:
            raise ValueError(f"invalid genotype for {agent_id}: {errors}")
        self.store.append(
            EventType.AGENT_CREATED,
            {"parent_id": parent_id, "generation": generation, "genotype_version": GENOTYPE_VERSION,
             "genotype": genotype, "genotype_digest": digest(genotype), "budgets": dict(self.config.farm["agent_budgets"]),
             "origin": origin, "status": "queued", "mutations": mutations or []},
            agent_id=agent_id, lineage_id=lineage_id, generation_id=generation,
            idempotency_key=f"create:{agent_id}",
        )

    def bootstrap(self, seed_genotypes: list[dict[str, Any]] | None = None) -> None:
        """Create Generation Zero if the ledger is empty."""
        if self.state.generations:
            return
        seed = self.config.seed
        rng = random.Random(int(digest([seed, "seed_population"])[:16], 16))
        seed_genotypes = seed_genotypes or self.config.farm.get("seed_population") or []
        with self.store.transaction():
            population = []
            for i in range(self.config.population_size):
                genotype = seed_genotypes[i] if i < len(seed_genotypes) else self.random_genotype(rng)
                agent_id = stable_id(seed, "agent", 0, i)
                self.create_agent(agent_id=agent_id, lineage_id=stable_id(seed, "lineage", 0, i), parent_id=None,
                                  generation=0, genotype=genotype, origin="seed")
                population.append(agent_id)
            self.start_generation(0, population)

    def start_generation(self, number: int, population: list[str]) -> None:
        cfg = self._fresh_generation_config()
        started = self.clock.now_dt()
        ends = started + timedelta(hours=float(self.config.farm["generation"]["duration_hours"]))
        rng = random.Random(int(digest([self.config.seed, "cohort", number])[:16], 16))
        frac = float(cfg["scheduler"].get("control_cohort_fraction", 0.0))
        k = round(frac * len(population))
        control = set(rng.sample(sorted(population), k)) if k else set()
        self.store.append(
            EventType.GENERATION_STARTED,
            {"number": number, "start_tick": self.clock.tick, "started_at": started.isoformat(),
             "ends_at": ends.isoformat(), "config": cfg, "population": sorted(population),
             "cohort": {a: ("control" if a in control else "treatment") for a in sorted(population)}},
            generation_id=number, idempotency_key=f"genstart:{number}",
        )
        engine = self.policy_engine()
        for agent_id in sorted(population):
            caps = self._capabilities_for(agent_id, engine)
            self.store.append(
                EventType.CAPABILITY_ISSUED,
                {"token_id": self._token_id(agent_id, number), "capabilities": caps,
                 "epoch": self.state.capability_epoch, "policy_fingerprint": self.current_policy_fingerprint()},
                agent_id=agent_id, lineage_id=self.state.agents[agent_id].lineage_id, generation_id=number,
                idempotency_key=f"cap:{number}:{agent_id}:{self.state.capability_epoch}",
            )

    def _capabilities_for(self, agent_id: str, engine: PolicyEngine) -> list[str]:
        prefs = set(self.state.agents[agent_id].genotype["tool_preferences"])
        return sorted(prefs & set(engine.granted_capabilities()))

    def _token_id(self, agent_id: str, generation: int) -> str:
        return stable_id(self.config.seed, "token", agent_id, generation, self.state.capability_epoch)

    def token_for(self, agent_id: str, generation: int) -> str:
        return self.authority.issue(
            token_id=self._token_id(agent_id, generation), agent_id=agent_id, generation_id=generation,
            capabilities=self._capabilities_for(agent_id, self.policy_engine()), epoch=self.state.capability_epoch,
            policy_fingerprint=self.current_policy_fingerprint(),
        )

    def workspace_for(self, agent_id: str) -> Path:
        return self.data_dir / "workspaces" / agent_id

    # ============================================================ recovery
    def recover(self) -> None:
        """Bring a restarted supervisor back to a consistent state."""
        with self.store.transaction():
            for job_id, job in sorted(self.state.jobs_inflight.items()):
                self.store.append(
                    EventType.INFERENCE_JOB_CANCELLED, {"job_id": job_id, "reason": "supervisor restart"},
                    agent_id=job["agent_id"], generation_id=job["generation"],
                    idempotency_key=f"jobcancel:{job_id}",
                )
        self.resume_interrupted_close()

    def resume_interrupted_close(self) -> int | None:
        for number in sorted(self.state.generations):
            g = self.state.generations[number]
            if g.closing and not g.closed:
                self.generations.close(number)
                return number
        return None

    # ================================================================ ticks
    def run(self, *, generations: int | None = None, ticks: int | None = None) -> list[TickResult]:
        self.bootstrap()
        start_gen = self.state.current_generation or 0
        results = []
        n = 0
        while True:
            if ticks is not None and n >= ticks:
                break
            if generations is not None and self.state.current_generation >= start_gen + generations:
                break
            r = self.tick()
            results.append(r)
            n += 1
            if r.halted:
                break
            self.clock.sleep_until_next_tick()
        return results

    def tick(self) -> TickResult:
        self._process_inbox()
        tick = self.clock.tick
        gen = self.state.current_generation
        result = TickResult(tick=tick, generation=gen)
        if self.state.halted:
            result.halted = True
            return result
        if gen is None:
            self.bootstrap()
            gen = self.state.current_generation
            result.generation = gen
        if self.resume_interrupted_close() is not None:
            result.notes.append("resumed interrupted generation close")
            return result
        gv = self.state.generations[gen]
        now = self.clock.now_dt()
        if now >= parse_ts(gv.ends_at):
            self.close_generation()
            result.closed_generation = gen
            return result

        with self.store.transaction():
            self.payments.reconcile(now, gen, tick)
            self._execute_granted_approvals(gen, tick)
            self._enforce_status(gen)

        reason = None
        health = self.inference.health()
        if health.ok != self._last_backend_ok:
            self.store.append(EventType.HEALTH_EVENT, {"component": "inference", "kind": "backend_health",
                                                       "ok": health.ok, "detail": health.detail})
            self._last_backend_ok = health.ok
        if not health.ok and not self.config.farm["inference"].get("escalation", {}).get("enabled"):
            reason = f"inference unhealthy: {health.detail}"
        sched_cfg = self.generation_config()["scheduler"]
        if farm_gpu_last_day(self.state, tick, self.ticks_per_day) >= float(sched_cfg.get("daily_gpu_seconds_ceiling", float("inf"))):
            reason = "daily inference ceiling reached"
        candidates = [] if reason else self._candidates(gen, tick)
        slots = int(self.config.farm["inference"].get("slots_per_tick", 4))
        decision = self.scheduler_for(gen).allocate(tick, gen, candidates, slots, list(self.state.allocations),
                                                    reason=reason)

        submitted: list[tuple[str, str, int, dict[str, Any]]] = []
        with self.store.transaction():
            self.store.append(EventType.SCHEDULER_ALLOCATION, decision.to_payload(), generation_id=gen,
                              idempotency_key=f"alloc:{tick}")
            for sel in decision.selected:
                agent = self.state.agents[sel.agent_id]
                version, st, recovered = self.runtime.load_state(agent.id)
                if recovered:
                    self.store.append(EventType.HEALTH_EVENT,
                                      {"component": "agent_state", "kind": "state_corrupted", "detail": "reset"},
                                      agent_id=agent.id, lineage_id=agent.lineage_id, generation_id=gen)
                request = self.runtime.build_request(agent, st, self.config.seed)
                counters = self.state.counter(gen, agent.id)
                remaining_tokens = float(agent.budgets.get("inference_tokens", 1e12)) - counters.tokens
                step_id = self.ids.new("step")
                job_id = self.inference.submit(agent.id, request, sel.priority,
                                               {"max_tokens": max(1, int(remaining_tokens))}, job_id=self.ids.new("job"))
                self.store.append(
                    EventType.INFERENCE_JOB_SUBMITTED,
                    {"job_id": job_id, "step_id": step_id, "tick": tick, "priority": round(sel.priority, 6),
                     "pool": sel.pool, "max_tokens": request.max_tokens, "input_digest": digest(request.messages)},
                    agent_id=agent.id, lineage_id=agent.lineage_id, generation_id=gen,
                    idempotency_key=f"jobsubmit:{job_id}",
                )
                submitted.append((job_id, step_id, version, st))
        result.scheduled = len(submitted)

        by_job = {s[0]: s for s in submitted}
        for job in self.inference.run_queued():
            job_id, step_id, version, st = by_job[job.job_id]
            agent = self.state.agents[job.agent_id]
            try:
                with self.store.transaction():
                    if job.state != "done":
                        self.store.append(
                            EventType.INFERENCE_JOB_FAILED,
                            {"job_id": job_id, "step_id": step_id, "tick": tick, "error": job.error or job.state},
                            agent_id=agent.id, lineage_id=agent.lineage_id, generation_id=gen,
                            idempotency_key=f"jobfail:{job_id}",
                        )
                        self.store.append(EventType.HEALTH_EVENT,
                                          {"component": "inference", "kind": "job_failed", "detail": job.error},
                                          agent_id=agent.id, lineage_id=agent.lineage_id, generation_id=gen)
                        result.failed += 1
                        continue
                    self._complete_step(agent, gen, tick, job, step_id, version, st)
                    result.completed += 1
            except Exception as exc:  # a bug in one step must not take down the farm
                self.store.append(EventType.HEALTH_EVENT,
                                  {"component": "supervisor", "kind": "step_exception", "detail": repr(exc)[:500]},
                                  agent_id=agent.id, lineage_id=agent.lineage_id, generation_id=gen)
                result.failed += 1
        self.clock.set_tick(tick + 1)
        return result

    def _complete_step(self, agent, gen: int, tick: int, job, step_id: str, version: int, st: dict[str, Any]) -> None:
        usage = job.usage
        now = self.clock.now_dt()
        self.store.append(
            EventType.INFERENCE_JOB_COMPLETED,
            {"job_id": job.job_id, "step_id": step_id, "tick": tick, "usage": usage.to_dict(),
             "output_digest": digest(job.text or "")},
            agent_id=agent.id, lineage_id=agent.lineage_id, generation_id=gen,
            idempotency_key=f"jobdone:{job.job_id}",
        )
        attr = Attribution(agent_id=agent.id, lineage_id=agent.lineage_id, generation_id=gen, step_id=step_id,
                           tick=tick, now=now)
        self.compute.charge(attr, job.job_id, usage.gpu_seconds, usage.cloud_usd)
        step = StepContext(agent_id=agent.id, lineage_id=agent.lineage_id, generation_id=gen, step_id=step_id,
                           tick=tick, now=now, workspace=self.workspace_for(agent.id))
        outcome = self.runtime.execute(agent, self.token_for(agent.id, gen), step, job.text or "", version, st)
        self.store.append(
            EventType.AGENT_STEP_COMPLETED,
            {"step_id": step_id, "job_id": job.job_id, "tick": tick, "actions": outcome.actions,
             "denied": outcome.denied, "malformed": outcome.malformed, "error": outcome.error,
             "state_version": outcome.state_version, "state_digest": outcome.state_digest},
            agent_id=agent.id, lineage_id=agent.lineage_id, generation_id=gen,
            idempotency_key=f"step:{step_id}",
        )
        if outcome.malformed:
            self.store.append(EventType.HEALTH_EVENT,
                              {"component": "agent", "kind": "malformed_output", "detail": outcome.error},
                              agent_id=agent.id, lineage_id=agent.lineage_id, generation_id=gen)

    def _candidates(self, gen: int, tick: int) -> list[Candidate]:
        gv = self.state.generations[gen]
        fcfg = self.generation_config(gen)["fitness"]
        prior_default = float(fcfg.get("posterior", {}).get("prior_mean_per_step", 0.0))
        running = [a for a in self.state.agents.values() if a.status == "running"]
        gpu_known = [self.state.counter(gen, a.id) for a in running]
        gpu_known = [c.gpu_seconds / c.steps for c in gpu_known if c.steps]
        default_gpu = sum(gpu_known) / len(gpu_known) if gpu_known else 2.0
        out = []
        for a in sorted(running, key=lambda a: a.id):
            c = self.state.counter(gen, a.id)
            budget = check_agent_budget(a.budgets, c)
            if not budget.ok:
                self.store.append(
                    EventType.HEALTH_EVENT,
                    {"component": "budget", "kind": "budget_exhausted", "detail": ",".join(budget.exhausted)},
                    agent_id=a.id, lineage_id=a.lineage_id, generation_id=gen,
                    idempotency_key=f"budget:{gen}:{a.id}",
                )
                continue
            prev = self.state.fitness.get(gen - 1, {}).get(a.id)
            prior = prev["posterior_mean_per_step"] if prev and prev.get("posterior_mean_per_step") is not None else prior_default
            out.append(Candidate(
                agent_id=a.id, lineage_id=a.lineage_id, steps=c.steps, net=c.net_realized,
                step_values=list(c.step_net.values()),
                gpu_per_step=c.gpu_seconds / c.steps if c.steps and c.gpu_seconds else default_gpu,
                last_scheduled_tick=a.last_scheduled_tick, burst=a.burst,
                cohort=gv.cohort.get(a.id, "treatment"), prior_mean=prior, generation_start_tick=gv.start_tick,
            ))
        return out

    def _enforce_status(self, gen: int) -> None:
        threshold = int(self.config.farm["runtime"].get("malformed_output_threshold", 8))
        for a in list(self.state.agents.values()):
            if a.status not in ("running", "queued", "paused"):
                continue
            c = self.state.counter(gen, a.id)
            if c.violations:
                self._disqualify(a.id, gen, c.violations[-1].get("reason", "policy violation"))
            elif a.status == "running" and c.consecutive_malformed >= threshold:
                self.store.append(EventType.AGENT_STATUS_CHANGED,
                                  {"status": "paused", "reason": f"health: {c.consecutive_malformed} malformed outputs"},
                                  agent_id=a.id, lineage_id=a.lineage_id, generation_id=gen)

    def _on_hard_violation(self, agent_id: str, generation: int, reason: str) -> None:
        if self.generation_config()["policy"].get("on_hard_violation", "disqualify_agent") == "disqualify_agent":
            self._disqualify(agent_id, generation, reason)

    def _disqualify(self, agent_id: str, generation: int, reason: str) -> None:
        a = self.state.agents[agent_id]
        if a.status == "disqualified":
            return
        self.store.append(EventType.AGENT_STATUS_CHANGED, {"status": "disqualified", "reason": reason},
                          agent_id=agent_id, lineage_id=a.lineage_id, generation_id=generation)

    def close_generation(self, *, crash_after: str | None = None) -> int:
        gen = self.state.current_generation
        self.generations.close(gen, crash_after=crash_after)
        return gen

    # ========================================================= approvals
    def resolve_approval(self, approval_id: str, *, granted: bool, operator: str, note: str = "") -> None:
        approval = self.state.approvals.get(approval_id)
        with self.store.transaction():
            approvals_mod.resolve(
                self.store, self.state, approval_id, granted=granted, operator=operator, note=note,
                policy_fingerprint=self.current_policy_fingerprint(),
            )
            agent = self.state.agents.get(approval["agent_id"]) if approval else None
            if agent:
                self.labor.charge(Attribution(agent.id, agent.lineage_id, approval["generation"], None,
                                              self.clock.tick, self.clock.now_dt()), approval_id)

    def _execute_granted_approvals(self, gen: int, tick: int) -> None:
        for a in list(self.state.approvals.values()):
            if a["status"] != "granted" or a["generation"] != gen:
                continue
            agent = self.state.agents.get(a["agent_id"])
            if agent is None or agent.status != "running":
                continue
            req = a["request"]
            step = StepContext(agent_id=agent.id, lineage_id=agent.lineage_id, generation_id=gen,
                               step_id=req.get("step_id"), tick=tick, now=self.clock.now_dt(),
                               workspace=self.workspace_for(agent.id))
            self.gateway.invoke(self.token_for(agent.id, gen), req["tool"], req["args"], step,
                                approval_id=a["approval_id"])

    # ===================================================== emergency stop
    def emergency_stop(self, reason: str, *, operator: str = "operator") -> None:
        """Halt population work and revoke every issued capability."""
        for job_id in list(self.state.jobs_inflight):
            self.inference.cancel(job_id)
        with self.store.transaction():
            self.store.append(EventType.EMERGENCY_STOP, {"reason": reason, "operator": operator},
                              author=f"operator:{operator}", generation_id=self.state.current_generation)
            self.store.append(EventType.CAPABILITIES_REVOKED,
                              {"scope": "all", "epoch": self.state.capability_epoch + 1, "reason": reason},
                              generation_id=self.state.current_generation)
            self.store.append(EventType.HUMAN_INTERVENTION, {"kind": "emergency_stop", "operator": operator,
                                                              "detail": reason},
                              author=f"operator:{operator}", generation_id=self.state.current_generation)
            for job_id, job in sorted(self.state.jobs_inflight.items()):
                self.store.append(EventType.INFERENCE_JOB_CANCELLED, {"job_id": job_id, "reason": "emergency stop"},
                                  agent_id=job["agent_id"], generation_id=job["generation"],
                                  idempotency_key=f"jobcancel:{job_id}")

    def resume(self, *, operator: str = "operator") -> None:
        gen = self.state.current_generation
        with self.store.transaction():
            self.store.append(EventType.FARM_RESUMED, {"operator": operator}, author=f"operator:{operator}",
                              generation_id=gen)
            self.store.append(EventType.HUMAN_INTERVENTION, {"kind": "resume", "operator": operator},
                              author=f"operator:{operator}", generation_id=gen)
            if gen is not None:
                engine = self.policy_engine()
                for a in self.state.active_agents():
                    self.store.append(
                        EventType.CAPABILITY_ISSUED,
                        {"token_id": self._token_id(a.id, gen), "capabilities": self._capabilities_for(a.id, engine),
                         "epoch": self.state.capability_epoch,
                         "policy_fingerprint": self.current_policy_fingerprint()},
                        agent_id=a.id, lineage_id=a.lineage_id, generation_id=gen,
                        idempotency_key=f"cap:{gen}:{a.id}:{self.state.capability_epoch}",
                    )

    # ============================================================ inbox
    @property
    def inbox(self) -> Path:
        return self.data_dir / "inbox"

    def _process_inbox(self) -> None:
        """Operator commands delivered as files, so the supervisor stays the single writer."""
        if not self.inbox.exists():
            return
        done = self.inbox / "processed"
        for path in sorted(self.inbox.glob("*.json")):
            try:
                cmd = json.loads(path.read_text())
                kind = cmd.get("command")
                operator = str(cmd.get("operator", "operator"))
                if kind == "stop":
                    self.emergency_stop(cmd.get("reason", "operator stop"), operator=operator)
                elif kind == "resume":
                    self.resume(operator=operator)
                elif kind in ("approve", "deny"):
                    self.resolve_approval(cmd["approval_id"], granted=kind == "approve", operator=operator,
                                          note=cmd.get("note", ""))
                else:
                    raise ValueError(f"unknown command {kind!r}")
                outcome = "ok"
            except Exception as exc:
                outcome = f"error: {exc}"
                self.store.append(EventType.HEALTH_EVENT, {"component": "inbox", "kind": "command_failed",
                                                           "detail": f"{path.name}: {exc}"[:500]})
            done.mkdir(parents=True, exist_ok=True)
            path.rename(done / f"{path.stem}.{'ok' if outcome == 'ok' else 'failed'}.json")


def submit_command(data_dir: Path, command: dict[str, Any]) -> Path:
    """Queue an operator command for a running supervisor."""
    import time
    import uuid

    inbox = Path(data_dir) / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    path = inbox / f"{time.time_ns()}-{uuid.uuid4().hex[:8]}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(command))
    tmp.rename(path)
    return path
