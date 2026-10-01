"""Matched evolved-vs-frozen-control experiment.

The treatment uses Qwenomatic's normal selection/reproduction loop. The
control uses the identical seed population, market, scheduler and inference
configuration but sets retirement and immigration to zero, so the same agents
carry forward unchanged. If a control agent becomes ineligible/health-failed,
the arm is marked invalid rather than silently replacing it.

Generation 0 is the pre-treatment baseline: evolution only changes the
population after that generation closes. The primary effect therefore uses
Generations 1+.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
import time

from storage.events import EventStore, FarmState
from supervisor.config import FarmConfig
from supervisor.core import Supervisor


@dataclass
class GenerationSummary:
    generation: int
    steps: int
    tokens: int
    gpu_seconds: float
    gross_revenue: float
    net_realized: float
    external_spend: float
    conversions: int
    retired: int
    offspring: int
    immigrants: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ArmSummary:
    arm: str
    data_dir: str
    generations: list[GenerationSummary]

    def to_dict(self) -> dict[str, Any]:
        rows = [g.to_dict() for g in self.generations]
        total_steps = sum(g.steps for g in self.generations)
        total_gpu = sum(g.gpu_seconds for g in self.generations)
        total_net = sum(g.net_realized for g in self.generations)
        post = self.generations[1:]
        post_steps = sum(g.steps for g in post)
        post_gpu = sum(g.gpu_seconds for g in post)
        post_net = sum(g.net_realized for g in post)
        return {
            "arm": self.arm,
            "data_dir": self.data_dir,
            "generations": rows,
            "total": {
                "steps": total_steps,
                "gpu_seconds": round(total_gpu, 6),
                "net_realized": round(total_net, 6),
                "net_per_call": round(total_net / total_steps, 9) if total_steps else None,
                "profit_per_gpu_hour": round(total_net / (total_gpu / 3600), 6) if total_gpu else None,
            },
            "post_baseline": {
                "generations": [g.generation for g in post],
                "steps": post_steps,
                "gpu_seconds": round(post_gpu, 6),
                "net_realized": round(post_net, 6),
                "net_per_call": round(post_net / post_steps, 9) if post_steps else None,
                "profit_per_gpu_hour": round(post_net / (post_gpu / 3600), 6) if post_gpu else None,
            },
        }


def arm_config(
    data_dir: Path,
    *,
    arm: str,
    backend: str,
    base_url: str | None = None,
    model: str | None = None,
    max_concurrency: int = 2,
    seed: int | None = None,
) -> FarmConfig:
    if arm not in ("treatment", "control"):
        raise ValueError("arm must be treatment or control")
    overrides: dict[str, Any] = {"farm": {"inference": {"backend": backend}}}
    if seed is not None:
        overrides["farm"]["farm"] = {"seed": int(seed)}
    if backend == "openai_compatible":
        oc: dict[str, Any] = {"max_concurrency": int(max_concurrency)}
        if base_url:
            oc["base_url"] = base_url
        if model:
            oc["model"] = model
        overrides["farm"]["inference"]["openai_compatible"] = oc
    if arm == "control":
        overrides["farm"]["evolution"] = {
            "retire_fraction": 0.0,
            "immigration_rate": 0.0,
        }
    return FarmConfig.load(data_dir=data_dir, overrides=overrides)


def summarize(data_dir: Path, arm: str, generations: int) -> ArmSummary:
    store = EventStore(data_dir / "ledger.sqlite3", read_only=True)
    try:
        state = FarmState(cap_window=512).replay(store.iter_events())
        out: list[GenerationSummary] = []
        for g in range(generations):
            gv = state.generations.get(g)
            if gv is None or not gv.closed:
                raise RuntimeError(f"{arm}: generation {g} is not closed")
            cs = state.counters[g].values()
            plan = state.selections[g]["plan"]
            out.append(GenerationSummary(
                generation=g,
                steps=sum(c.steps for c in cs),
                tokens=sum(c.tokens for c in cs),
                gpu_seconds=sum(c.gpu_seconds for c in cs),
                gross_revenue=sum(c.gross_revenue for c in cs),
                net_realized=sum(c.net_realized for c in cs),
                external_spend=sum(c.external_spend for c in cs),
                conversions=sum(c.conversions for c in cs),
                retired=len(plan.get("retirements", [])),
                offspring=sum(o.get("origin") == "offspring" for o in plan.get("offspring", [])),
                immigrants=sum(o.get("origin") == "immigrant" for o in plan.get("offspring", [])),
            ))
        return ArmSummary(arm=arm, data_dir=str(data_dir), generations=out)
    finally:
        store.close()


def _assert_frozen_control(sup: Supervisor, through_generation: int) -> None:
    for g in range(through_generation):
        plan = sup.state.selections[g]["plan"]
        if plan.get("retirements") or plan.get("offspring"):
            raise RuntimeError(
                "control arm became non-frozen in generation "
                f"{g}: retirements={len(plan.get('retirements', []))}, "
                f"offspring={len(plan.get('offspring', []))}. "
                "This paired run is invalid; inspect health/policy events."
            )


def run_arm(
    *,
    arm: str,
    data_dir: Path,
    generations: int,
    backend: str,
    base_url: str | None = None,
    model: str | None = None,
    max_concurrency: int = 2,
    seed: int | None = None,
    progress_every_ticks: int = 12,
) -> ArmSummary:
    cfg = arm_config(
        data_dir,
        arm=arm,
        backend=backend,
        base_url=base_url,
        model=model,
        max_concurrency=max_concurrency,
        seed=seed,
    )
    sup = Supervisor(cfg)
    try:
        sup.bootstrap()
        health = sup.inference.health()
        if not health.ok:
            raise RuntimeError(f"{arm}: inference backend unhealthy: {health.detail}")

        target_generation = int(generations)
        last_generation = sup.state.current_generation or 0
        ticks = 0
        wall_started = time.monotonic()
        print(f"\n== {arm}: target {generations} closed generations ==")
        while (sup.state.current_generation or 0) < target_generation:
            result = sup.tick()
            ticks += 1
            if result.halted:
                raise RuntimeError(f"{arm}: farm halted: {sup.state.halt_reason}")
            current = sup.state.current_generation or 0
            if current != last_generation:
                closed = current - 1
                cs = sup.state.counters[closed].values()
                plan = sup.state.selections[closed]["plan"]
                print(
                    f"{arm} generation {closed} closed | "
                    f"net {sum(c.net_realized for c in cs):.2f} | "
                    f"steps {sum(c.steps for c in cs)} | "
                    f"retired {len(plan.get('retirements', []))} | "
                    f"offspring {sum(o.get('origin') == 'offspring' for o in plan.get('offspring', []))}"
                )
                last_generation = current
                if arm == "control":
                    _assert_frozen_control(sup, current)
            elif progress_every_ticks and ticks % progress_every_ticks == 0:
                gv = sup.state.generations[current]
                gen_tick = sup.clock.tick - gv.start_tick
                elapsed = max(0.001, time.monotonic() - wall_started)
                rate = ticks / elapsed
                remaining_ticks = max(0, (target_generation - current - 1) * cfg.ticks_per_generation
                                      + (cfg.ticks_per_generation - gen_tick))
                eta_minutes = remaining_ticks / rate / 60 if rate > 0 else None
                print(
                    f"{arm} gen {current}: tick {gen_tick}/{cfg.ticks_per_generation} | "
                    f"wall {elapsed/60:.1f}m | ETA "
                    f"{eta_minutes:.1f}m" if eta_minutes is not None else
                    f"{arm} gen {current}: tick {gen_tick}/{cfg.ticks_per_generation}"
                )

        if arm == "control":
            _assert_frozen_control(sup, target_generation)
    finally:
        sup.close()

    return summarize(data_dir, arm, generations)


def compare(treatment: ArmSummary, control: ArmSummary) -> dict[str, Any]:
    if len(treatment.generations) != len(control.generations):
        raise ValueError("arms have different generation counts")

    per_generation = []
    for t, c in zip(treatment.generations, control.generations):
        per_generation.append({
            "generation": t.generation,
            "treatment_net": round(t.net_realized, 6),
            "control_net": round(c.net_realized, 6),
            "delta_net": round(t.net_realized - c.net_realized, 6),
            "treatment_steps": t.steps,
            "control_steps": c.steps,
            "equal_call_count": t.steps == c.steps,
        })

    td, cd = treatment.to_dict(), control.to_dict()
    tp, cp = td["post_baseline"], cd["post_baseline"]
    delta = tp["net_realized"] - cp["net_realized"]
    relative = None
    if cp["net_realized"] != 0:
        relative = delta / abs(cp["net_realized"])

    t0, c0 = treatment.generations[0], control.generations[0]
    t0_rpc = t0.net_realized / t0.steps if t0.steps else 0.0
    c0_rpc = c0.net_realized / c0.steps if c0.steps else 0.0
    tpost_rpc = tp["net_per_call"] or 0.0
    cpost_rpc = cp["net_per_call"] or 0.0
    did_rpc = (tpost_rpc - t0_rpc) - (cpost_rpc - c0_rpc)

    return {
        "design": {
            "primary_outcome": "post-baseline net realized profit",
            "baseline_generation": 0,
            "treatment": "normal selection + cloning + bounded mutation",
            "control": "same seed population carried forward unchanged",
        },
        "treatment": td,
        "control": cd,
        "per_generation": per_generation,
        "baseline_balance": {
            "treatment_net": round(t0.net_realized, 6),
            "control_net": round(c0.net_realized, 6),
            "delta_net": round(t0.net_realized - c0.net_realized, 6),
            "treatment_net_per_call": round(t0_rpc, 9),
            "control_net_per_call": round(c0_rpc, 9),
        },
        "primary_effect": {
            "delta_net_realized": round(delta, 6),
            "difference_in_differences_net_per_call": round(did_rpc, 9),
            "relative_to_abs_control": round(relative, 6) if relative is not None else None,
            "treatment_net_per_call": tp["net_per_call"],
            "control_net_per_call": cp["net_per_call"],
            "delta_net_per_call": (
                round(tp["net_per_call"] - cp["net_per_call"], 9)
                if tp["net_per_call"] is not None and cp["net_per_call"] is not None else None
            ),
            "all_generation_call_counts_equal": all(r["equal_call_count"] for r in per_generation),
        },
    }


def write_report(root: Path, report: dict[str, Any]) -> tuple[Path, Path]:
    root.mkdir(parents=True, exist_ok=True)
    json_path = root / "report.json"
    md_path = root / "report.md"
    json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    effect = report["primary_effect"]
    rows = [
        "# Qwenomatic Evolution A/B",
        "",
        "Generation 0 is the matched pre-treatment baseline. The primary comparison uses Generations 1+.",
        "",
        "| Gen | Treatment net | Control net | Delta | Calls equal |",
        "|---:|---:|---:|---:|:---:|",
    ]
    for r in report["per_generation"]:
        rows.append(
            f"| {r['generation']} | {r['treatment_net']:.2f} | {r['control_net']:.2f} | "
            f"{r['delta_net']:+.2f} | {'yes' if r['equal_call_count'] else 'NO'} |"
        )
    rows += [
        "",
        "## Primary post-baseline effect",
        "",
        f"- Treatment net: {report['treatment']['post_baseline']['net_realized']:.2f}",
        f"- Control net: {report['control']['post_baseline']['net_realized']:.2f}",
        f"- Raw post-baseline delta: {effect['delta_net_realized']:+.2f}",
        f"- Baseline-adjusted difference-in-differences per call: "
        f"{effect['difference_in_differences_net_per_call']:+.6f}",
        f"- Generation-0 baseline delta: {report['baseline_balance']['delta_net']:+.2f}",
        f"- Equal call counts in every generation: {effect['all_generation_call_counts_equal']}",
        "",
        "This single paired run is evidence, not a statistical conclusion. Repeat with additional seeds before "
        "attributing a durable effect to evolution.",
    ]
    md_path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return json_path, md_path


def aggregate_reports(reports: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate independent paired trials without pretending small-N is certainty."""
    if not reports:
        raise ValueError("at least one paired report is required")

    deltas = [float(r["primary_effect"]["delta_net_realized"]) for r in reports]
    dids = [float(r["primary_effect"]["difference_in_differences_net_per_call"]) for r in reports]
    ordered = sorted(deltas)
    mid = len(ordered) // 2
    median = ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2
    ordered_did = sorted(dids)
    mid_did = len(ordered_did) // 2
    median_did = (
        ordered_did[mid_did]
        if len(ordered_did) % 2
        else (ordered_did[mid_did - 1] + ordered_did[mid_did]) / 2
    )
    return {
        "pairs": len(reports),
        "positive_raw_delta_pairs": sum(d > 0 for d in deltas),
        "positive_baseline_adjusted_pairs": sum(d > 0 for d in dids),
        "mean_raw_delta_net": round(sum(deltas) / len(deltas), 6),
        "median_raw_delta_net": round(median, 6),
        "mean_difference_in_differences_net_per_call": round(sum(dids) / len(dids), 9),
        "median_difference_in_differences_net_per_call": round(median_did, 9),
        "all_call_counts_equal": all(
            bool(r["primary_effect"]["all_generation_call_counts_equal"]) for r in reports
        ),
        "pair_effects": [
            {
                "pair": i + 1,
                "raw_delta_net": round(deltas[i], 6),
                "difference_in_differences_net_per_call": round(dids[i], 9),
                "baseline_delta_net": round(float(reports[i]["baseline_balance"]["delta_net"]), 6),
            }
            for i in range(len(reports))
        ],
    }


def write_campaign_report(root: Path, reports: list[dict[str, Any]]) -> tuple[Path, Path]:
    summary = aggregate_reports(reports)
    payload = {"summary": summary, "pairs": reports}
    json_path = root / "campaign-report.json"
    md_path = root / "campaign-report.md"
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    rows = [
        "# Qwenomatic Evolution A/B Campaign",
        "",
        f"Paired trials completed: {summary['pairs']}",
        "",
        "| Pair | Raw post-baseline delta | Baseline-adjusted delta/call | Baseline imbalance |",
        "|---:|---:|---:|---:|",
    ]
    for p in summary["pair_effects"]:
        rows.append(
            f"| {p['pair']} | {p['raw_delta_net']:+.2f} | "
            f"{p['difference_in_differences_net_per_call']:+.6f} | "
            f"{p['baseline_delta_net']:+.2f} |"
        )
    rows += [
        "",
        "## Aggregate",
        "",
        f"- Positive raw-delta pairs: {summary['positive_raw_delta_pairs']}/{summary['pairs']}",
        f"- Positive baseline-adjusted pairs: "
        f"{summary['positive_baseline_adjusted_pairs']}/{summary['pairs']}",
        f"- Mean raw post-baseline delta: {summary['mean_raw_delta_net']:+.2f}",
        f"- Median raw post-baseline delta: {summary['median_raw_delta_net']:+.2f}",
        f"- Mean baseline-adjusted delta/call: "
        f"{summary['mean_difference_in_differences_net_per_call']:+.6f}",
        f"- Median baseline-adjusted delta/call: "
        f"{summary['median_difference_in_differences_net_per_call']:+.6f}",
        f"- Equal call counts in every paired run: {summary['all_call_counts_equal']}",
        "",
        "Interpretation: repeated paired trials reduce the chance that one lucky model trajectory is mistaken "
        "for an evolutionary effect. This is still an experiment in a simulated market, not evidence of "
        "real-world profitability.",
    ]
    md_path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return json_path, md_path
