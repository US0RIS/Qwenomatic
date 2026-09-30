"""Versioned, auditable fitness evaluation (DESIGN §9).

Objective: expected incremental net realized profit per GPU-hour, shrunk
toward a prior and reported with its uncertainty. Selection uses the bounds:
elites need a high lower bound (confidently good), retirement needs a low
upper bound (confidently bad), and parents are weighted by the posterior mean.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from storage.events import AgentCounters, AgentView

from ..config import config_hash
from .eligibility import Eligibility
from .stats import shrink

DISQUALIFIED = "DISQUALIFIED"


@dataclass
class FitnessResult:
    agent_id: str
    lineage_id: str
    generation: int
    archetype: str
    window_generations: list[int]
    eligible: bool
    eligibility_reasons: list[str]
    health_failed: bool
    fitness: float | str
    fitness_sd: float | None
    fitness_lcb: float | None
    fitness_ucb: float | None
    posterior_mean_per_step: float | None
    posterior_sd_per_step: float | None
    steps: int
    gpu_seconds: float
    tokens: int
    opportunities: int
    conversions: int
    gross_revenue: float
    refunds: float
    total_costs: float
    net_realized: float
    unrealized: float
    milestone_value: float
    human_interventions: int
    profit_per_gpu_hour: float | None
    profit_per_external_dollar: float | None
    age_generations: int
    min_exposure_met: bool
    fitness_version: int
    fitness_config_hash: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class FitnessEvaluator:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.version = int(config.get("version", 1))
        self.config_hash = config_hash(config)
        post = config.get("posterior", {})
        self.prior_mean = float(post.get("prior_mean_per_step", 0.0))
        self.prior_strength = float(post.get("prior_strength_steps", 10))
        self.variance_floor = float(post.get("variance_floor", 0.25))
        self.risk_aversion = float(config.get("risk_aversion", 1.0))
        self.milestone_weight = float(config.get("milestone_weight", 0.0))
        self.min_exposure_steps = int(config.get("min_exposure_steps", 0))
        self.archetypes = config.get("archetypes", {"standard": {"window_generations": 1, "min_age_generations": 0}})

    def archetype_params(self, archetype: str) -> dict[str, int]:
        return self.archetypes.get(archetype, self.archetypes.get("standard", {}))

    def window(self, archetype: str, generation: int, born: int) -> list[int]:
        w = int(self.archetype_params(archetype).get("window_generations", 1))
        return list(range(max(born, generation - w + 1), generation + 1))

    def evaluate(self, agent: AgentView, window_counters: AgentCounters, *, archetype: str, generation: int,
                 window: list[int], eligibility: Eligibility) -> FitnessResult:
        c = window_counters
        age = generation - agent.generation_born
        min_age = int(self.archetype_params(archetype).get("min_age_generations", 0))
        exposure_met = c.steps >= self.min_exposure_steps and age >= min_age
        net = c.net_realized + self.milestone_weight * c.milestone_value
        base = dict(
            agent_id=agent.id, lineage_id=agent.lineage_id, generation=generation, archetype=archetype,
            window_generations=window, eligible=eligibility.eligible, eligibility_reasons=eligibility.reasons,
            health_failed=eligibility.health_failed, steps=c.steps, gpu_seconds=round(c.gpu_seconds, 6),
            tokens=c.tokens, opportunities=c.opportunities, conversions=c.conversions,
            gross_revenue=round(c.gross_revenue, 6), refunds=round(c.refunds, 6),
            total_costs=round(c.total_costs, 6), net_realized=round(c.net_realized, 6),
            unrealized=round(c.unrealized, 6), milestone_value=round(c.milestone_value, 6),
            human_interventions=c.human_interventions,
            profit_per_gpu_hour=round(c.net_realized / (c.gpu_seconds / 3600), 6) if c.gpu_seconds else None,
            profit_per_external_dollar=round(c.net_realized / c.external_spend, 6) if c.external_spend else None,
            age_generations=age, min_exposure_met=exposure_met, fitness_version=self.version,
            fitness_config_hash=self.config_hash,
        )
        if not eligibility.eligible:
            # Revenue cannot offset a violation: no economic score is computed.
            return FitnessResult(fitness=DISQUALIFIED, fitness_sd=None, fitness_lcb=None, fitness_ucb=None,
                                 posterior_mean_per_step=None, posterior_sd_per_step=None, **base)
        post = shrink(net, c.steps, list(c.step_net.values()), prior_mean=self.prior_mean,
                      prior_strength=self.prior_strength, variance_floor=self.variance_floor)
        steps_per_gpu_hour = 3600.0 / (c.gpu_seconds / c.steps) if c.steps and c.gpu_seconds else 0.0
        fitness = post.mean * steps_per_gpu_hour
        fitness_sd = post.sd * steps_per_gpu_hour
        return FitnessResult(
            fitness=round(fitness, 6), fitness_sd=round(fitness_sd, 6),
            fitness_lcb=round(fitness - self.risk_aversion * fitness_sd, 6),
            fitness_ucb=round(fitness + self.risk_aversion * fitness_sd, 6),
            posterior_mean_per_step=round(post.mean, 6), posterior_sd_per_step=round(post.sd, 6), **base,
        )
