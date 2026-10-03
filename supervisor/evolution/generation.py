"""Generation close as a resumable transaction (DESIGN §10.1).

Each step commits a GENERATION_CLOSE_STEP marker together with its side
effects. A crash between steps resumes from the last committed step using
the data recorded there, so selection never runs twice for one snapshot and
no generation is started twice (DESIGN §15, I9). Until the activation step
commits, the previous population is intact.
"""

from __future__ import annotations

import random
import copy
import math
from collections import Counter
from typing import TYPE_CHECKING, Any

from storage.events import EventType, FarmState, digest

from ..evaluator import FitnessEvaluator, causal_report, evaluate_eligibility
from ..ids import stable_id
from .diversity import diversity_report
from .mutation import MutationError
from .selection import plan_selection

if TYPE_CHECKING:  # pragma: no cover
    from ..core import Supervisor

CLOSE_STEPS = (
    "stop_admission",
    "checkpoint_jobs",
    "reconcile",
    "freeze_ledger",
    "evaluate_eligibility",
    "calculate_fitness",
    "select_elites",
    "select_parents",
    "determine_retirements",
    "generate_mutations",
    "validate_offspring",
    "write_lineage",
    "activate_next_population",
    "resume_scheduling",
)


class SimulatedCrash(Exception):
    """Raised by tests to kill a close between two committed steps."""


def seeded_rng(*key: object) -> random.Random:
    return random.Random(int(digest(list(key))[:16], 16))


def snapshot_state(sup: "Supervisor", snapshot_seq: int) -> FarmState:
    state = FarmState()
    return state.replay(sup.store.iter_events(upto_seq=snapshot_seq))


def evaluate_generation(sup: "Supervisor", state: FarmState, generation: int) -> dict[str, dict[str, Any]]:
    """Eligibility + fitness for every non-retired agent, from a frozen state."""
    gen = state.generations[generation]
    evaluator = FitnessEvaluator(gen.config["fitness"])
    results = {}
    for agent in sorted(state.agents.values(), key=lambda a: a.id):
        if agent.status == "retired":
            continue
        archetype = sup.config.archetype_of(agent.genotype["target"]["segment"])
        window = evaluator.window(archetype, generation, agent.generation_born)
        counters = state.window_counters(agent.id, window)
        elig = evaluate_eligibility(agent, counters)
        if agent.role != 'business':
            from ..roles import specialist_fitness
            results[agent.id] = specialist_fitness(agent, counters, elig, generation,
                                                   sup.store.iter_events(upto_seq=state.last_seq))
            continue
        results[agent.id] = evaluator.evaluate(agent, counters, archetype=archetype, generation=generation,
                                               window=window, eligibility=elig).to_dict()
    from ..improvements import crowd_results
    return crowd_results(results, state, generation)


def selection_for(sup: "Supervisor", state: FarmState, generation: int,
                  results: dict[str, dict[str, Any]]) -> dict[str, Any]:
    gen = state.generations[generation]
    lineage_of = {a: state.agents[a].lineage_id for a in results}
    rng = seeded_rng(sup.config.seed, "selection", generation)
    cfg = copy.deepcopy(gen.config['evolution'])
    from ..roles import frozen_layout, plan_specialists
    role_cfg = frozen_layout(state, generation)
    business_results = {a: r for a, r in results.items() if state.agents[a].role == 'business'}
    shifts = sup.store.iter_events(types=[EventType.MARKET_SHIFT], upto_seq=state.last_seq,
                                  generation_ids=[generation])
    if shifts and gen.config.get('improvements', {}).get('adaptation', {}).get('enabled'):
        cfg['immigration_rate'] = shifts[-1].payload['immigration_rate']
    plan = plan_selection(business_results, lineage_of, population_size=role_cfg['counts']['business'],
                          cfg=cfg, rng=rng)
    if shifts:
        plan['market_shift_seq'] = shifts[-1].seq
    from ..improvements import spread_plan, settings
    plan = spread_plan(plan, business_results, state, gen.config.get('improvements', settings()), role_cfg['counts']['business'])
    if 'roles' not in gen.config and generation not in state.role_layouts:
        return plan  # Historical pre-role selections must replay byte-for-byte.
    return plan_specialists(plan, results, state, role_cfg)


class GenerationManager:
    def __init__(self, sup: "Supervisor") -> None:
        self.sup = sup

    # ---------------------------------------------------------------- close
    def close(self, generation: int, *, crash_after: str | None = None) -> None:
        sup = self.sup
        for index, step in enumerate(CLOSE_STEPS):
            done = sup.state.generations[generation].close_steps
            if step in done:
                continue
            with sup.store.transaction():
                data = getattr(self, f"_step_{step}")(generation, done)
                sup.store.append(
                    EventType.GENERATION_CLOSE_STEP, {"step": step, "index": index, "data": data},
                    generation_id=generation, idempotency_key=f"close:{generation}:{step}",
                )
            if crash_after == step:
                raise SimulatedCrash(step)

    def _step_stop_admission(self, g: int, done: dict[str, Any]) -> dict[str, Any]:
        return {"at_tick": self.sup.clock.tick, "at": self.sup.clock.now()}

    def _step_checkpoint_jobs(self, g: int, done: dict[str, Any]) -> dict[str, Any]:
        sup = self.sup
        cancelled = []
        for job_id, job in sorted(sup.state.jobs_inflight.items()):
            checkpoint = sup.inference.checkpoint(job_id)
            sup.inference.cancel(job_id)
            sup.store.append(
                EventType.INFERENCE_JOB_CANCELLED,
                {"job_id": job_id, "reason": "generation close", "checkpoint": checkpoint},
                agent_id=job["agent_id"], generation_id=job["generation"],
                idempotency_key=f"jobcancel:{job_id}",
            )
            cancelled.append(job_id)
        return {"cancelled": cancelled}

    def _step_reconcile(self, g: int, done: dict[str, Any]) -> dict[str, Any]:
        sup = self.sup
        settled = sup.payments.reconcile(sup.clock.now_dt(), g, sup.clock.tick)
        return {"settled": settled, "pending_after": len(sup.state.pending_settlements)}

    def _step_freeze_ledger(self, g: int, done: dict[str, Any]) -> dict[str, Any]:
        from ..roles import layout
        from ..improvements import emit
        emit(self.sup, EventType.ROLE_LAYOUT_PLANNED, {'layout': layout(self.sup.config.farm)},
             f'role-layout:{g}', generation=g)
        seq, h = self.sup.store.head()
        return {"snapshot_seq": seq, "snapshot_hash": h}

    def _frozen(self, g: int) -> FarmState:
        snap = self.sup.state.generations[g].close_steps["freeze_ledger"]
        cache = getattr(self, "_snapshot_cache", None)
        if cache and cache[0] == snap["snapshot_seq"]:
            return cache[1]
        state = snapshot_state(self.sup, snap["snapshot_seq"])
        self._snapshot_cache = (snap["snapshot_seq"], state)
        return state

    def _step_evaluate_eligibility(self, g: int, done: dict[str, Any]) -> dict[str, Any]:
        state = self._frozen(g)
        out = {}
        for agent in sorted(state.agents.values(), key=lambda a: a.id):
            if agent.status == "retired":
                continue
            evaluator = FitnessEvaluator(state.generations[g].config["fitness"])
            archetype = self.sup.config.archetype_of(agent.genotype["target"]["segment"])
            window = evaluator.window(archetype, g, agent.generation_born)
            e = evaluate_eligibility(agent, state.window_counters(agent.id, window))
            out[agent.id] = {"eligible": e.eligible, "reasons": e.reasons, "health_failed": e.health_failed}
        return {"eligibility": out}

    def _step_calculate_fitness(self, g: int, done: dict[str, Any]) -> dict[str, Any]:
        sup = self.sup
        state = self._frozen(g)
        results = evaluate_generation(sup, state, g)
        for agent_id, r in results.items():
            sup.store.append(
                EventType.FITNESS_EVALUATED, r, agent_id=agent_id, lineage_id=r["lineage_id"], generation_id=g,
                idempotency_key=f"fitness:{g}:{agent_id}",
            )
        report = causal_report(state, g)
        sup.store.append(EventType.ATTRIBUTION_REPORT, report, generation_id=g, idempotency_key=f"attribution:{g}")
        cfg = state.generations[g].config["fitness"]
        return {"evaluated": len(results), "fitness_version": cfg.get("version"),
                "fitness_config_hash": state.generations[g].config["hashes"]["fitness"]}

    def _step_select_elites(self, g: int, done: dict[str, Any]) -> dict[str, Any]:
        sup = self.sup
        snap = done["freeze_ledger"]
        existing = sup.store.get_by_idempotency(f"selection:{snap['snapshot_hash']}")
        if existing is None:
            results = {a: r for a, r in sup.state.fitness[g].items()}
            plan = selection_for(sup, self._frozen(g), g, results)
            sup.store.append(
                EventType.SELECTION_DECIDED,
                {"snapshot_seq": snap["snapshot_seq"], "snapshot_hash": snap["snapshot_hash"], "plan": plan},
                generation_id=g, idempotency_key=f"selection:{snap['snapshot_hash']}",
            )
        plan = sup.state.selections[g]["plan"]
        return {"elites": plan["elites"]}

    def _plan(self, g: int) -> dict[str, Any]:
        return self.sup.state.selections[g]["plan"]

    def _step_select_parents(self, g: int, done: dict[str, Any]) -> dict[str, Any]:
        plan = self._plan(g)
        return {"parents": [o["parent_id"] for o in plan["offspring"] if o["parent_id"]],
                "immigrants": sum(1 for o in plan["offspring"] if o["origin"] == "immigrant"),
                "parent_weights": plan["parent_weights"]}

    def _step_determine_retirements(self, g: int, done: dict[str, Any]) -> dict[str, Any]:
        return {"retirements": self._plan(g)["retirements"]}

    def _step_generate_mutations(self, g: int, done: dict[str, Any]) -> dict[str, Any]:
        sup = self.sup
        plan = self._plan(g)
        seed = sup.config.seed
        offspring = []
        cfg = sup.generation_config(g)['improvements']
        from .mutation import MutationEngine
        mutations = MutationEngine(sup.generation_config(g)['mutation'], segments=list(sup.config.segments),
                                   granted_tools=sorted(sup.config.policy['capabilities']),
                                   max_mutations=int(sup.generation_config(g)['evolution'].get('max_mutations', 2)))
        counts = Counter(sup.state.agents[a].genotype['target']['segment'] for a in plan['survivors']
                         if sup.state.agents[a].role == 'business')
        cap = plan.get('segment_cap', plan.get('role_layout', {}).get('counts', {}).get('business', sup.config.population_size))
        immigrants = [o for o in plan['offspring'] if o['origin'] == 'immigrant']
        archive_slots = set()
        archives = []
        if cfg['archive']['enabled'] and (not cfg['archive']['shift_only'] or bool(sup.store.iter_events(
                types=[EventType.MARKET_SHIFT], upto_seq=done.get('freeze_ledger', {}).get('snapshot_seq'), generation_ids=[g]))):
            archives = [e for e in sup.store.iter_events(types=[EventType.RETIREMENT_REPORT]) if e.payload['safe_to_return']]
            if archives:
                rng_archive = seeded_rng(seed, 'archive', g)
                archive_slots = set(rng_archive.sample([o['slot'] for o in immigrants],
                    math.floor(len(immigrants) * cfg['archive']['max_return_share'])))
        for o in plan["offspring"]:
            child_id = stable_id(seed, "agent", g + 1, o["slot"])
            rng = seeded_rng(seed, "mutation", g, o["slot"])
            if o['origin'] == 'specialist':
                from ..roles import specialist_genotype
                offspring.append({'agent_id': child_id, 'lineage_id': stable_id(seed, 'lineage', g+1, o['slot']),
                    'parent_id': None, 'origin': 'specialist', 'mutations': [], 'role': o['role'],
                    'role_slot': o['role_slot'], 'genotype': specialist_genotype(sup, o['role'], o['role_slot'])})
                continue
            if o["origin"] == "offspring":
                parent = sup.state.agents[o["parent_id"]]
                try:
                    source = copy.deepcopy(parent.genotype)
                    second = None
                    if cfg['crossover']['enabled'] and rng.random() < cfg['crossover']['probability']:
                        pool = [a for a in plan['ranked'] if sup.state.agents[a].lineage_id != parent.lineage_id
                                and sup.state.fitness[g][a]['fitness_lcb'] > 0]
                        if pool and sup.state.fitness[g][parent.id]['fitness_lcb'] > 0:
                            second = sup.state.agents[rng.choice(pool)]
                            source['strategy_prompt'] = second.genotype['strategy_prompt']
                            source['workflow'] = second.genotype['workflow']
                    genotype, diffs = mutations.mutate(source, rng)
                    entry = {"agent_id": child_id, "lineage_id": parent.lineage_id, "parent_id": parent.id,
                             "origin": "offspring", "genotype": genotype, "mutations": [d.to_dict() for d in diffs],
                             "second_parent_id": second.id if second else None}
                    if second:
                        entry['mutations'].insert(0, {'kind': 'crossover', 'second_parent_id': second.id,
                                                     'fields': ['strategy_prompt', 'workflow']})
                except MutationError as exc:
                    entry = {"agent_id": child_id, "origin": "offspring", "parent_id": parent.id,
                             "lineage_id": parent.lineage_id, "genotype": None, "error": str(exc), "mutations": []}
            else:
                entry = {"agent_id": child_id, "lineage_id": stable_id(seed, "lineage", g + 1, o["slot"]),
                         "parent_id": None, "origin": "immigrant", "mutations": [],
                         "genotype": sup.random_genotype(rng)}
                if o['slot'] in archive_slots:
                    report = rng.choice(archives)
                    entry['genotype'] = copy.deepcopy(report.payload['genotype'])
                    entry['origin'] = 'archive_return'
                    entry['mutations'] = [{'kind': 'archive_return', 'source_report_id': report.event_id,
                                           'source_agent_id': report.agent_id}]
            if entry.get('genotype') and cfg['knowledge']['enabled']:
                segment = entry['genotype']['target']['segment']
                if counts[segment] >= cap:
                    available = sorted(s for s in sup.config.segments if counts[s] < cap)
                    if not available:
                        raise ValueError('segment spread cap infeasible')
                    new_segment = rng.choice(available)
                    entry['genotype']['target']['segment'] = new_segment
                    entry['mutations'].append({'kind': 'segment_spread', 'old': segment, 'new': new_segment})
                    segment = new_segment
                counts[segment] += 1
            offspring.append(entry)
        return {"offspring": offspring}

    def _step_validate_offspring(self, g: int, done: dict[str, Any]) -> dict[str, Any]:
        valid, invalid = [], []
        for o in done["generate_mutations"]["offspring"]:
            if o.get('role', 'business') != 'business':
                from runtime.agent import validate_genotype
                errors = validate_genotype(o['genotype'], mutation_cfg={**self.sup.config.farm['mutation'], 'required_tools': []},
                                           segments=list(self.sup.config.segments), granted_tools=[])
            else:
                errors = [o["error"]] if o.get("error") else self.sup.mutations.validate(o["genotype"])
            (invalid if errors else valid).append(o["agent_id"] if not errors else
                                                  {"agent_id": o["agent_id"], "errors": errors})
        return {"valid": valid, "invalid": invalid}

    def _step_write_lineage(self, g: int, done: dict[str, Any]) -> dict[str, Any]:
        sup = self.sup
        valid = set(done["validate_offspring"]["valid"])
        n = 0
        for o in done["generate_mutations"]["offspring"]:
            if o["agent_id"] not in valid:
                continue
            sup.store.append(
                EventType.MUTATION_APPLIED,
                {"child_id": o["agent_id"], "parent_id": o["parent_id"], "origin": o["origin"],
                 "diffs": o["mutations"], "genotype_digest": digest(o["genotype"])},
                agent_id=o["agent_id"], lineage_id=o["lineage_id"], generation_id=g,
                idempotency_key=f"mutation:{o['agent_id']}",
            )
            n += 1
        return {"written": n}

    def _step_activate_next_population(self, g: int, done: dict[str, Any]) -> dict[str, Any]:
        sup = self.sup
        plan = self._plan(g)
        for r in plan["retirements"]:
            agent = sup.state.agents[r["agent_id"]]
            if agent.role == 'business':
                sup.improvements.archive(agent, g, r['reason'])
            sup.store.append(EventType.AGENT_RETIRED, {"reason": r["reason"]}, agent_id=agent.id,
                             lineage_id=agent.lineage_id, generation_id=g, idempotency_key=f"retire:{g}:{agent.id}")
        valid = set(done["validate_offspring"]["valid"])
        created = []
        for o in done["generate_mutations"]["offspring"]:
            if o["agent_id"] not in valid:
                continue
            sup.create_agent(agent_id=o["agent_id"], lineage_id=o["lineage_id"], parent_id=o["parent_id"],
                             generation=g + 1, genotype=o["genotype"], origin=o["origin"], mutations=o["mutations"],
                             role=o.get('role', 'business'), role_slot=o.get('role_slot'))
            created.append(o["agent_id"])
        population = sorted(set(plan["survivors"]) | set(created))
        from ..roles import frozen_layout
        sup.start_generation(g + 1, population, role_layout=plan.get('role_layout', frozen_layout(self._frozen(g), g)))
        return {"population": population, "created": created, "retired": [r["agent_id"] for r in plan["retirements"]],
                "diversity": diversity_report(sup.state.agents[a] for a in population)}

    def _step_resume_scheduling(self, g: int, done: dict[str, Any]) -> dict[str, Any]:
        self.sup.store.append(EventType.GENERATION_CLOSED, {"number": g, "next": g + 1}, generation_id=g,
                              idempotency_key=f"genclosed:{g}")
        return {"next_generation": g + 1}
