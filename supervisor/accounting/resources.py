"""Per-agent resource budgets and farm-level ceilings (DESIGN §7.5)."""

from __future__ import annotations

from dataclasses import dataclass

from storage.events import AgentCounters, FarmState


@dataclass
class BudgetCheck:
    ok: bool
    exhausted: list[str]


def check_agent_budget(budgets: dict[str, float], counters: AgentCounters) -> BudgetCheck:
    usage = {
        "inference_tokens": counters.tokens,
        "gpu_seconds": counters.gpu_seconds,
        "external_spend": counters.external_spend,
        "tool_calls": counters.tool_calls,
    }
    exhausted = [k for k, used in usage.items() if k in budgets and used >= float(budgets[k])]
    return BudgetCheck(ok=not exhausted, exhausted=exhausted)


def remaining_spend(budgets: dict[str, float], counters: AgentCounters) -> float:
    return max(0.0, float(budgets.get("external_spend", 0.0)) - counters.external_spend)


def trailing_sum(by_tick: dict[int, float], current_tick: int, window_ticks: int) -> float:
    lo = current_tick - window_ticks
    return sum(v for t, v in by_tick.items() if lo < t <= current_tick)


def farm_gpu_last_day(state: FarmState, current_tick: int, ticks_per_day: int) -> float:
    return trailing_sum(state.gpu_by_tick, current_tick, ticks_per_day)


def farm_spend_last_day(state: FarmState, current_tick: int, ticks_per_day: int) -> float:
    return trailing_sum(state.spend_by_tick, current_tick, ticks_per_day)
