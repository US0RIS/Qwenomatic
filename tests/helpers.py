"""Shared helpers for building small, fast, deterministic farms."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from supervisor.config import FarmConfig, deep_merge
from supervisor.core import Supervisor

# Six-hour generations of 15-minute ticks: 24 ticks per generation.
FAST = {
    "farm": {
        "generation": {"duration_hours": 6, "tick_seconds": 900},
        "scheduler": {"cap_window_ticks": 12, "starvation_ticks": 6, "max_burst_ticks": 8},
    },
    "fitness": {"min_exposure_steps": 5},
}


def make_config(tmp_path: Path, overrides: dict[str, Any] | None = None, *, fast: bool = True,
                segments: list[str] | None = None) -> FarmConfig:
    # Existing mechanism tests intentionally use business-only populations.
    # Role integration/acceptance tests explicitly enable the shipped split.
    base = deep_merge(FAST if fast else {}, {'farm': {'roles': {'enabled': False}}})
    merged = deep_merge(base, overrides or {})
    cfg = FarmConfig.load(data_dir=tmp_path / "farm", overrides=merged)
    if segments is not None:
        econ, market = cfg.farm["economy"]["segments"], cfg.farm["simulation"]["market"]["segments"]
        cfg.farm["economy"]["segments"] = {s: econ[s] for s in segments}
        cfg.farm["simulation"]["market"]["segments"] = {s: market[s] for s in segments}
    return cfg


def make_supervisor(tmp_path: Path, overrides: dict[str, Any] | None = None, *, fast: bool = True,
                    seed_genotypes: list[dict[str, Any]] | None = None, bootstrap: bool = True,
                    segments: list[str] | None = None, **kw) -> Supervisor:
    sup = Supervisor(make_config(tmp_path, overrides, fast=fast, segments=segments), **kw)
    if bootstrap:
        sup.bootstrap(seed_genotypes)
    return sup


def reopen(sup: Supervisor, **kw) -> Supervisor:
    cfg = sup.config
    sup.close()
    return Supervisor(cfg, **kw)


def genotype(segment: str = "freelancer-templates", price: float = 20.0, *, prompt: str | None = None,
             workflow: str = "offer_first", tools: list[str] | None = None, temperature: float = 0.3,
             survey_every: int = 6) -> dict[str, Any]:
    return {
        "strategy_prompt": prompt or "Lead with a concrete, verifiable benefit.",
        "workflow": workflow,
        "target": {"segment": segment},
        "pricing_parameters": {"price": price},
        "tool_preferences": sorted(tools or ["market.offer", "market.survey", "memory.note"]),
        "planning_parameters": {"temperature": temperature, "max_tokens": 512, "survey_every": survey_every},
    }


def run_generations(sup: Supervisor, n: int) -> None:
    target = sup.state.current_generation + n
    guard = 0
    while sup.state.current_generation < target:
        sup.tick()
        guard += 1
        assert guard < 100_000, "farm did not advance"


def run_ticks(sup: Supervisor, n: int) -> None:
    for _ in range(n):
        sup.tick()
