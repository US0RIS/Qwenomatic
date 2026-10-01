"""Supervisor-side control of model thinking (EXPERIMENTS.md E04/E05, invariants 12-15).

A reasoning model may skip thinking on a step only while the agent stays
inside the operating region the supervisor validated at its last deep
(thinking-on) step. Everything here is computed from supervisor- and
adapter-authored ledger events; the model's own text, confidence or claims
never enter a decision.

Safety order, strongest first:

1. **Hard cadence.** At most ``deep_every - 1`` thinking-off steps run
   between two validations. Nothing below can lengthen this interval: the
   detectors only ever add reasons to think.
2. **Early-warning detectors**, each measured against the *fixed* anchor
   captured at the last validation, never against a trailing window:
   state distance (OOD), supervisor-computed uncertainty, and one-sided
   CUSUM outcome drift.
3. **Known-event signatures**: first step, every Nth step, malformed prior
   output, blocked action, tool error. Supplemental, not primary.

Thinking events take ids derived from their step instead of the shared
deterministic id stream, so recording telemetry never changes the
simulated world (market draws are keyed on invocation ids) and A/B arms
that record different numbers of anchors stay comparable.

Every detector component is scaled so that 1.0 means "at its configured
limit"; a step escalates when any component reaches the threshold (1.0 by
default). Each decision is written to the ledger as a THINKING_DECISION
event with every component, so an escalation can always be explained.

The decision changes only the inference request. Thinking-on and
thinking-off steps produce text that goes through the same
``AgentRuntime.execute`` -> capability gateway -> policy engine -> budget
checks -> trusted adapters path; no fast path exists here.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from statistics import median
from typing import Any

from storage.events import EventStore, EventType, FarmState

from .ids import stable_id

MODES = ("server", "on", "off", "adaptive")

# Outcome metrics, each a per-action mean with weight = actions in the step.
# Sign: +1 means a *higher* value is adverse (refunds), -1 means lower is adverse.
METRICS: dict[str, int] = {
    "value_per_action": -1,   # booked revenue - external spend - fees - refunds, per offer
    "conversion": -1,         # booked sales per offer
    "spend_efficiency": -1,   # booked revenue per external dollar
    "refund_rate": +1,        # realized refunds per offer
}

DEFAULTS: dict[str, Any] = {
    "mode": "server",
    "first_steps": 1,
    # Hard cadence, the primary bound: a deep review at least every `deep_every`
    # steps, i.e. at most deep_every - 1 thinking-off steps in a row. Detectors
    # may force an earlier review; they can never extend this interval.
    "deep_every": 8,
    "after_trouble": True,
    "anchor_window_steps": 8,    # observations summarized into a validation anchor
    "current_window_steps": 4,   # recent observations measured against the anchor
    "min_actions": 6,            # outcome components need this many offers in the current window
    "ood": {
        "threshold": 1.0,
        "price_log_tolerance": math.log(1.25),  # +/-25% price move = 1.0
        "segment_share_tolerance": 0.34,        # share of offers outside the anchor's segments
        "tool_mix_tolerance": 0.5,              # total-variation distance between tool mixes
        "spend_per_action_rel_tolerance": 0.5,
        "metric_rel_tolerance": {"value_per_action": 0.5, "conversion": 0.5,
                                 "spend_efficiency": 0.5, "refund_rate": 1.0},
    },
    "uncertainty": {
        "threshold": 1.0,
        "min_anchor_actions": 6,     # an anchor resting on fewer offers is not a validated region
        "rel_se_limit": 1.0,         # standard error of value/action relative to its mean
        "surprise_z_limit": 3.0,     # realized vs anchor-expected value since validation
        "budget_boundary": 0.2,      # remaining spend or token budget fraction
    },
    "drift": {
        "cusum_k": 0.25,             # allowance, in anchor standard deviations per offer
        "cusum_h": 4.0,              # decision interval; drift score = S / h
        "sd_floor_rel": 0.25,
    },
    # Absolute floors for metric scales, so a near-zero anchor mean does not
    # turn noise into infinite relative change.
    "metric_floor": {"value_per_action": 1.0, "conversion": 0.05, "spend_efficiency": 1.0, "refund_rate": 0.02},
}


def _merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in (over or {}).items():
        out[k] = _merge(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return out


# --------------------------------------------------------------- observation
@dataclass
class Observation:
    """What the ledger recorded for one agent between two harvests."""

    actions: int = 0
    conversions: int = 0
    booked_revenue: float = 0.0
    spend: float = 0.0
    fees: float = 0.0
    refunds: int = 0
    refund_amount: float = 0.0
    tools: dict[str, int] = field(default_factory=dict)
    segments: dict[str, int] = field(default_factory=dict)
    prices: list[float] = field(default_factory=list)
    malformed: bool = False
    denied: int = 0
    tool_errors: int = 0
    upto_seq: int = 0

    @property
    def value(self) -> float:
        return self.booked_revenue - self.spend - self.fees - self.refund_amount

    def metric(self, name: str) -> float | None:
        if self.actions <= 0:
            return None
        if name == "value_per_action":
            return self.value / self.actions
        if name == "conversion":
            return self.conversions / self.actions
        if name == "refund_rate":
            return self.refunds / self.actions
        if name == "spend_efficiency":
            return self.booked_revenue / self.spend if self.spend > 0 else None
        raise KeyError(name)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["prices"] = [round(p, 4) for p in self.prices]
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Observation":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


def observe(events: list[Any]) -> Observation:
    """Fold supervisor/adapter-authored events into one observation. Agent claims are not read."""
    o = Observation()
    for e in events:
        p = e.payload
        if e.type == EventType.OPPORTUNITY:
            o.actions += 1
            seg = str(p.get("segment"))
            o.segments[seg] = o.segments.get(seg, 0) + 1
            if isinstance(p.get("price"), (int, float)):
                o.prices.append(float(p["price"]))
        elif e.type == EventType.FINANCIAL_EVENT:
            amount, status = float(p["amount"]), p.get("status")
            if p["type"] == "revenue" and not p.get("settles_reference"):
                o.conversions += 1  # a booked sale; its later settlement is not a new conversion
                o.booked_revenue += amount
            elif p["type"] == "refund" and status == "realized":
                o.refunds += 1
                o.refund_amount += amount
            elif p["type"] == "fee" and status == "realized":
                o.fees += amount
            elif p["type"] == "expense" and p.get("category") == "external_spend":
                o.spend += amount
        elif e.type == EventType.TOOL_INVOKED:
            tool = str(p.get("tool"))
            o.tools[tool] = o.tools.get(tool, 0) + 1
            if p.get("status") == "error":
                o.tool_errors += 1
        elif e.type == EventType.AGENT_STEP_COMPLETED:
            o.malformed = o.malformed or bool(p.get("malformed"))
            o.denied += int(p.get("denied", 0))
        o.upto_seq = max(o.upto_seq, e.seq)
    return o


HARVEST_TYPES = [EventType.OPPORTUNITY, EventType.FINANCIAL_EVENT, EventType.TOOL_INVOKED,
                 EventType.AGENT_STEP_COMPLETED]


# ------------------------------------------------------------------ summaries
def summarize(observations: list[Observation]) -> dict[str, Any]:
    """Window statistics. Per-action variance is pooled from step means: E[w (m - mu)^2] = var."""
    segments: dict[str, int] = {}
    tools: dict[str, int] = {}
    prices: list[float] = []
    for o in observations:
        for k, v in o.segments.items():
            segments[k] = segments.get(k, 0) + v
        for k, v in o.tools.items():
            tools[k] = tools.get(k, 0) + v
        prices.extend(o.prices)
    metrics: dict[str, dict[str, float | int | None]] = {}
    for name in METRICS:
        rows = [(o.actions, o.metric(name)) for o in observations]
        rows = [(w, m) for w, m in rows if m is not None and w > 0]
        weight = sum(w for w, _ in rows)
        if not weight:
            metrics[name] = {"mean": None, "sd": None, "weight": 0, "steps": 0}
            continue
        mu = sum(w * m for w, m in rows) / weight
        sd = math.sqrt(sum(w * (m - mu) ** 2 for w, m in rows) / (len(rows) - 1)) if len(rows) > 1 else None
        metrics[name] = {"mean": mu, "sd": sd, "weight": weight, "steps": len(rows)}
    actions = sum(o.actions for o in observations)
    spend = sum(o.spend for o in observations)
    return {
        "steps": len(observations),
        "actions": actions,
        "segments": segments,
        "tools": tools,
        "price": median(prices) if prices else None,
        "spend_per_action": spend / actions if actions else None,
        "metrics": metrics,
    }


def price_band(price: float | None) -> int | None:
    """Log-spaced price band (25% wide), so a regime is (segment, band)."""
    return None if not price or price <= 0 else int(math.floor(math.log(price) / math.log(1.25)))


def _tv(a: dict[str, int], b: dict[str, int]) -> float:
    ta, tb = sum(a.values()), sum(b.values())
    if not ta or not tb:
        return 0.0
    return 0.5 * sum(abs(a.get(k, 0) / ta - b.get(k, 0) / tb) for k in set(a) | set(b))


def _scale(cfg: dict[str, Any], name: str, mean: float | None) -> float:
    return max(abs(mean or 0.0), float(cfg["metric_floor"][name]))


def unit_sd(cfg: dict[str, Any], name: str, ref: dict[str, Any]) -> float:
    """Per-action standard deviation of a metric in a reference summary, floored."""
    m = ref["metrics"][name]
    floor = float(cfg["drift"]["sd_floor_rel"]) * _scale(cfg, name, m["mean"])
    return max(float(m["sd"] or 0.0), floor, 1e-9)


def state_distance(ref: dict[str, Any], cur: dict[str, Any], cfg: dict[str, Any]) -> dict[str, float]:
    """Component distances of a current window from a reference summary; 1.0 = at tolerance.

    The supervisor always passes the fixed validation anchor as `ref`. The
    function is pure so tests can show that the same metric over a sliding
    window (adjacent windows as ref/cur) stays quiet under slow drift.
    """
    o = cfg["ood"]
    comp: dict[str, float] = {}
    ref_segs = set(ref["segments"]) or {ref.get("genotype_segment")}
    if cur["actions"]:
        outside = sum(v for k, v in cur["segments"].items() if k not in ref_segs) / cur["actions"]
        comp["segment"] = outside / float(o["segment_share_tolerance"])
    if ref.get("price") and cur.get("price"):
        comp["price"] = abs(math.log(cur["price"] / ref["price"])) / float(o["price_log_tolerance"])
    if ref["tools"] and cur["tools"]:
        comp["tool_mix"] = _tv(ref["tools"], cur["tools"]) / float(o["tool_mix_tolerance"])
    if ref.get("spend_per_action") is not None and cur.get("spend_per_action") is not None:
        base = max(ref["spend_per_action"], 0.01)
        comp["spend_per_action"] = (abs(cur["spend_per_action"] - ref["spend_per_action"])
                                    / (float(o["spend_per_action_rel_tolerance"]) * base))
    if cur["actions"] >= int(cfg["min_actions"]):
        for name in METRICS:
            r, c = ref["metrics"][name], cur["metrics"][name]
            if r["mean"] is None or c["mean"] is None:
                continue
            # Tolerance plus two standard errors of the current window, so a
            # small sample's noise is not mistaken for a regime change.
            tol = float(o["metric_rel_tolerance"][name]) * _scale(cfg, name, r["mean"])
            se = unit_sd(cfg, name, ref) / math.sqrt(max(1, c["weight"]))
            comp[name] = abs(c["mean"] - r["mean"]) / (tol + 2 * se)
    return {k: round(v, 6) for k, v in comp.items()}


# ---------------------------------------------------------------- controller
@dataclass
class AgentControl:
    anchor: dict[str, Any] | None = None
    anchor_version: int = 0
    steps_since_validation: int = 0       # thinking-off steps since the anchor
    history: list[Observation] = field(default_factory=list)       # one observation per decision
    since_anchor: list[Observation] = field(default_factory=list)
    cusum: dict[str, float] = field(default_factory=lambda: {m: 0.0 for m in METRICS})
    validated_regimes: list[list[Any]] = field(default_factory=list)
    last_seq: int = 0
    # A deep step that completed with valid output. Its anchor is captured at
    # the agent's next decision, once its outcome is in the ledger. Memory
    # only: if lost (restart, rollback) the old anchor and its blind-step count
    # stay in force, which can only bring the next forced review closer.
    pending_validation: dict[str, Any] | None = None


@dataclass
class ThinkingDecision:
    thinking: bool | None
    reasons: list[str]
    trigger_class: str
    telemetry: dict[str, Any]


class ThinkingController:
    def __init__(self, store: EventStore, state: FarmState, config: dict[str, Any] | None) -> None:
        self.store = store
        self.state = state
        self.cfg = _merge(DEFAULTS, config or {})
        self.mode = self.cfg["mode"]
        if self.mode not in MODES:
            raise ValueError(f"runtime.thinking.mode must be one of {', '.join(MODES)}")
        self.deep_every = int(self.cfg["deep_every"])
        if self.mode == "adaptive" and self.deep_every < 1:
            raise ValueError("runtime.thinking.deep_every must be >= 1: the hard cadence cannot be disabled")
        self.window = int(self.cfg["anchor_window_steps"])
        self._agents: dict[str, AgentControl] = {}

    # ------------------------------------------------------------ state
    def reset(self) -> None:
        """Drop in-memory state; it is restored from the ledger on next use."""
        self._agents.clear()

    def _control(self, agent_id: str) -> AgentControl:
        ctl = self._agents.get(agent_id)
        if ctl is None:
            ctl = self._agents[agent_id] = self._restore(agent_id)
        return ctl

    def _restore(self, agent_id: str) -> AgentControl:
        """Rebuild controller state from this controller's own ledger events (restart, rollback)."""
        ctl = AgentControl()
        limit = 2 * (self.window + self.deep_every) + 8
        events = self.store.iter_events(types=[EventType.THINKING_DECISION, EventType.THINKING_ANCHOR],
                                        agent_id=agent_id, descending=True, limit=limit)
        for e in reversed(events):
            p = e.payload
            if e.type == EventType.THINKING_ANCHOR:
                self._install_anchor(ctl, p["anchor"], p["anchor_version"], p.get("validated_regimes", []))
                continue
            self._remember(ctl, Observation.from_dict(p["observation"]),
                           since_anchor=not p.get("observation_in_anchor"))
            ctl.steps_since_validation = int(p["steps_since_validation"])
            ctl.cusum = {m: float(v) for m, v in p["cusum"].items()}
            ctl.last_seq = int(p["observed_upto_seq"])
        return ctl

    def _remember(self, ctl: AgentControl, obs: Observation, *, since_anchor: bool) -> None:
        ctl.history = (ctl.history + [obs])[-self.window:]
        if since_anchor and ctl.anchor is not None:
            ctl.since_anchor = (ctl.since_anchor + [obs])[-(self.deep_every + self.window):]

    @staticmethod
    def _install_anchor(ctl: AgentControl, anchor: dict[str, Any], version: int, regimes: list[list[Any]]) -> None:
        ctl.anchor, ctl.anchor_version, ctl.validated_regimes = anchor, version, regimes
        ctl.since_anchor = []
        ctl.steps_since_validation = 0
        ctl.cusum = {m: 0.0 for m in METRICS}

    def _harvest(self, agent_id: str, ctl: AgentControl) -> Observation:
        events = self.store.iter_events(after_seq=ctl.last_seq, types=HARVEST_TYPES, agent_id=agent_id)
        obs = observe(events)
        obs.upto_seq = ctl.last_seq = max(obs.upto_seq, ctl.last_seq)
        return obs

    def _update_cusum(self, ctl: AgentControl, obs: Observation) -> None:
        """One-sided CUSUM per metric against the fixed anchor: S = max(0, S + w (z - k))."""
        k = float(self.cfg["drift"]["cusum_k"])
        for name, sign in METRICS.items():
            mu = ctl.anchor["metrics"][name]["mean"]
            m = obs.metric(name)
            if mu is None or m is None:
                continue
            z = sign * (m - mu) / unit_sd(self.cfg, name, ctl.anchor)  # adverse deviation is positive
            ctl.cusum[name] = max(0.0, ctl.cusum[name] + obs.actions * (z - k))

    def _capture_anchor(self, ctl: AgentControl, agent: Any, gen: int) -> None:
        """Freeze the validated operating region from observable state (never model output)."""
        g, deep = agent.genotype, ctl.pending_validation
        anchor = summarize(ctl.history)
        if anchor["price"] is None:
            anchor["price"] = float(g["pricing_parameters"]["price"])
        version = ctl.anchor_version + 1
        anchor.update({
            "anchor_id": f"{agent.id}:{version}", "generation": deep["generation"], "tick": deep["tick"],
            "step_id": deep["step_id"], "genotype_segment": g["target"]["segment"], "workflow": g["workflow"],
            "genotype_price": float(g["pricing_parameters"]["price"]),
        })
        segment = max(anchor["segments"], key=anchor["segments"].get) if anchor["segments"] else g["target"]["segment"]
        regime = [segment, price_band(anchor["price"])]
        regimes = ctl.validated_regimes + ([regime] if regime not in ctl.validated_regimes else [])
        self._install_anchor(ctl, anchor, version, regimes)
        ctl.pending_validation = None
        self.store.append(
            EventType.THINKING_ANCHOR,
            {"anchor": anchor, "anchor_version": version, "validated_regimes": regimes, "step_id": deep["step_id"]},
            agent_id=agent.id, lineage_id=agent.lineage_id, generation_id=gen,
            idempotency_key=f"anchor:{deep['step_id']}", event_id=stable_id("thinking_anchor", deep["step_id"]),
        )

    # ---------------------------------------------------------- detectors
    def _current_summary(self, ctl: AgentControl) -> dict[str, Any]:
        # A window without offers carries no price evidence; genotype price changes
        # are caught separately by the genotype_price component.
        return summarize(ctl.since_anchor[-int(self.cfg["current_window_steps"]):])

    def _ood(self, ctl: AgentControl, agent: Any, cur: dict[str, Any]) -> dict[str, Any]:
        a = ctl.anchor
        comp = state_distance(a, cur, self.cfg)
        g = agent.genotype
        # Supervisor-owned genotype fields: any change is a regime change.
        comp["genotype_segment"] = float(g["target"]["segment"] != a["genotype_segment"])
        comp["workflow"] = float(g["workflow"] != a["workflow"])
        comp["genotype_price"] = round(abs(math.log(float(g["pricing_parameters"]["price"]) / a["genotype_price"]))
                                       / float(self.cfg["ood"]["price_log_tolerance"]), 6)
        return {"score": max(comp.values(), default=0.0), "components": comp}

    def _uncertainty(self, ctl: AgentControl, agent: Any, cur: dict[str, Any], gen: int) -> dict[str, Any]:
        """Uncertainty from supervisor-observed quantities only; model self-confidence is never read."""
        u, a = self.cfg["uncertainty"], ctl.anchor
        comp: dict[str, float] = {}
        comp["anchor_sample"] = float(a["actions"] < int(u["min_anchor_actions"]))
        recent = summarize(ctl.history)["metrics"]["value_per_action"]
        if recent["weight"]:
            se = unit_sd(self.cfg, "value_per_action", {"metrics": {"value_per_action": recent}}) / math.sqrt(recent["weight"])
            comp["outcome_noise"] = round(se / _scale(self.cfg, "value_per_action", recent["mean"])
                                          / float(u["rel_se_limit"]), 6)
        since = summarize(ctl.since_anchor)["metrics"]["value_per_action"]
        mu = a["metrics"]["value_per_action"]["mean"]
        if since["weight"] and mu is not None:
            z = abs(since["mean"] - mu) / (unit_sd(self.cfg, "value_per_action", a) / math.sqrt(since["weight"]))
            comp["surprise"] = round(z / float(u["surprise_z_limit"]), 6)
        segment = max(cur["segments"], key=cur["segments"].get) if cur["segments"] else agent.genotype["target"]["segment"]
        price = cur["price"] if cur["price"] is not None else a["price"]
        comp["novel_regime"] = float([segment, price_band(price)] not in ctl.validated_regimes)
        c = self.state.counter(gen, agent.id)
        budgets = agent.budgets or {}
        remaining = []
        if budgets.get("external_spend"):
            remaining.append(1 - c.external_spend / float(budgets["external_spend"]))
        if budgets.get("inference_tokens"):
            remaining.append(1 - c.tokens / float(budgets["inference_tokens"]))
        comp["budget_boundary"] = float(bool(remaining) and min(remaining) < float(u["budget_boundary"]))
        return {"score": max(comp.values(), default=0.0), "components": comp}

    # ------------------------------------------------------------ public
    def decide(self, agent: Any, gen: int, tick: int, step_id: str, step_index: int) -> ThinkingDecision:
        """Decide whether this step may think and record why. Never reads model output."""
        ctl = self._control(agent.id)
        obs = self._harvest(agent.id, ctl)
        in_anchor = ctl.pending_validation is not None
        if in_anchor:
            # The deep step's own outcome is part of the region it validated.
            self._remember(ctl, obs, since_anchor=False)
            self._capture_anchor(ctl, agent, gen)
        else:
            if ctl.anchor is not None:
                self._update_cusum(ctl, obs)
            self._remember(ctl, obs, since_anchor=True)

        reasons: list[str] = []
        ood = unc = None
        drift = {m: round(s / float(self.cfg["drift"]["cusum_h"]), 6) for m, s in ctl.cusum.items()}
        if ctl.anchor is not None:
            cur = self._current_summary(ctl)
            ood = self._ood(ctl, agent, cur)
            unc = self._uncertainty(ctl, agent, cur, gen)

        # Known-event signatures (supplemental).
        if step_index < int(self.cfg["first_steps"]):
            reasons.append("first_step")
        if self.deep_every > 0 and step_index % self.deep_every == 0:
            reasons.append("periodic")
        if self.cfg["after_trouble"]:
            if obs.malformed:
                reasons.append("malformed_prior")
            if obs.denied:
                reasons.append("blocked_action")
            if obs.tool_errors:
                reasons.append("tool_error")
        # Primary bound. Compared with the configured constant only: no detector
        # value feeds into it, so healthy detectors cannot extend the interval.
        if ctl.anchor is None:
            reasons.append("no_anchor")
        if ctl.steps_since_validation >= self.deep_every - 1:
            reasons.append("hard_cadence")
        # Early-warning detectors, all against the fixed anchor. They only add reasons.
        if ood and ood["score"] >= float(self.cfg["ood"]["threshold"]):
            reasons.append("ood")
        if unc and unc["score"] >= float(self.cfg["uncertainty"]["threshold"]):
            reasons.append("uncertainty")
        reasons.extend(f"drift:{m}" for m, s in drift.items() if s >= 1.0)

        if self.mode == "server":
            thinking: bool | None = None
        elif self.mode == "adaptive":
            thinking = bool(reasons)
        else:
            thinking = self.mode == "on"
        if thinking is False:
            ctl.steps_since_validation += 1
        trigger_class = self._trigger_class(reasons) if thinking else "none"

        telemetry = {
            "step_id": step_id, "tick": tick, "step_index": step_index, "mode": self.mode, "thinking": thinking,
            "reasons": reasons, "trigger_class": trigger_class, "deep_every": self.deep_every,
            "steps_since_validation": ctl.steps_since_validation,
            "anchor_id": ctl.anchor["anchor_id"] if ctl.anchor else None, "anchor_version": ctl.anchor_version,
            "ood": ood, "uncertainty": unc, "drift": drift,
            "cusum": {m: round(v, 6) for m, v in ctl.cusum.items()},
            "observation": obs.to_dict(), "observation_in_anchor": in_anchor, "observed_upto_seq": ctl.last_seq,
        }
        self.store.append(EventType.THINKING_DECISION, telemetry, agent_id=agent.id, lineage_id=agent.lineage_id,
                          generation_id=gen, idempotency_key=f"think:{step_id}",
                          event_id=stable_id("thinking_decision", step_id))
        return ThinkingDecision(thinking, reasons, trigger_class, telemetry)

    @staticmethod
    def _trigger_class(reasons: list[str]) -> str:
        if "hard_cadence" in reasons:
            return "hard_cadence"
        if any(r in ("ood", "uncertainty") or r.startswith("drift:") for r in reasons):
            return "early_warning"
        return "known_event" if reasons else "none"

    def validate(self, agent: Any, gen: int, tick: int, step_id: str, *, malformed: bool) -> None:
        """Called after a thinking-on step. A malformed deep step validated nothing, so the
        previous anchor and its count of thinking-off steps stay in force."""
        if not malformed:
            self._control(agent.id).pending_validation = {"generation": gen, "tick": tick, "step_id": step_id}
