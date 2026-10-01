"""Supervisor thinking controller (supervisor/thinking.py; EXPERIMENTS.md E13, invariants 12-15).

The lab below writes the same supervisor/adapter-authored events a farm
writes, so the controller is exercised through its real ledger harvest.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from helpers import genotype
from storage.events import EventStore, EventType, FarmState, adapter_author
from supervisor.thinking import Observation, ThinkingController, state_distance, summarize


class Lab:
    def __init__(self, tmp_path, **cfg):
        self.store = EventStore(tmp_path / "ledger.sqlite3")
        self.ctl = ThinkingController(self.store, FarmState(), {"mode": "adaptive", **cfg})
        self.agent = SimpleNamespace(id="a1", lineage_id="L1", budgets={}, genotype=genotype(price=20.0))
        self.n = 0

    def outcome(self, step_id, *, actions=10, conversions=3, price=20.0, ad_cost=0.5,
                segment="freelancer-templates", malformed=False, denied=0, tool_error=False):
        common = dict(agent_id="a1", lineage_id="L1", generation_id=0)
        for i in range(actions):
            self.store.append(EventType.OPPORTUNITY, {"segment": segment, "price": price, "step_id": step_id},
                              author=adapter_author("market"), **common)
            self.store.append(EventType.TOOL_INVOKED, {"tool": "market.offer", "status": "ok", "step_id": step_id},
                              **common)
        if tool_error:
            self.store.append(EventType.TOOL_INVOKED, {"tool": "market.survey", "status": "error"}, **common)
        for i in range(conversions):
            self.store.append(EventType.FINANCIAL_EVENT, {"type": "revenue", "category": "gross_revenue",
                                                          "amount": price, "status": "realized"},
                              author=adapter_author("payments"), **common)
        if actions:
            self.store.append(EventType.FINANCIAL_EVENT, {"type": "expense", "category": "external_spend",
                                                          "amount": ad_cost * actions, "status": "realized"},
                              author=adapter_author("ad_platform"), **common)
        self.store.append(EventType.AGENT_STEP_COMPLETED,
                          {"step_id": step_id, "malformed": malformed, "denied": denied}, **common)

    def step(self, step_index=None, **outcome):
        """One farm step: decide, execute (outcome lands in the ledger), validate if it thought."""
        sid = f"s{self.n}"
        d = self.ctl.decide(self.agent, 0, self.n, sid, self.n if step_index is None else step_index)
        self.outcome(sid, **outcome)
        if d.thinking:
            self.ctl.validate(self.agent, 0, self.n, sid, malformed=outcome.get("malformed", False))
        self.n += 1
        return d


def steady(i):
    """Steady regime with realistic step-to-step variation: conversion 0.25 / 0.30 / 0.35 of 40 offers."""
    return {"actions": 40, "conversions": (10, 12, 14)[i % 3]}


def degrading(t):
    """Slow monotonic decline: -1.25 points of conversion per step, same variation, nothing else changes."""
    rate = 0.30 - 0.0125 * t
    return {"actions": 40, "conversions": max(0, round(40 * rate) + (-2, 0, 2)[t % 3])}


def _warm(lab, steps=12):
    for i in range(steps):
        lab.step(**steady(i))


# ------------------------------------------------------------ slow drift
def test_slow_drift_forces_deep_thinking_without_any_signature_event(tmp_path):
    # deep_every is huge so only the early-warning detectors can catch the drift.
    lab = Lab(tmp_path, deep_every=1000)
    _warm(lab)
    assert not lab.step(**steady(12)).thinking, "a validated steady regime runs thinking-off"
    onset, caught = lab.n, None
    for t in range(1, 25):
        d = lab.step(**degrading(t))
        if d.thinking:
            caught = (t, d)
            break
    assert caught is not None, "gradual degradation was never escalated"
    t, d = caught
    assert d.trigger_class == "early_warning"
    assert any(r == "ood" or r.startswith("drift:") for r in d.reasons), d.reasons
    # No signature fired: no malformed output, block, tool error or cadence.
    assert not {"malformed_prior", "blocked_action", "tool_error", "hard_cadence", "periodic"} & set(d.reasons)
    assert t <= 10, f"detection delay {t} steps after onset at step {onset}"


def _windows(obs, size):
    return [obs[i - size:i] for i in range(size, len(obs) + 1)]


def test_sliding_window_distance_alone_stays_quiet_where_the_fixed_anchor_does_not():
    def ob(actions, conversions, price=20.0, ad_cost=0.5):
        return Observation(actions=actions, conversions=conversions, booked_revenue=conversions * price,
                           spend=ad_cost * actions, tools={"market.offer": actions},
                           segments={"freelancer-templates": actions}, prices=[price] * actions)

    cfg = ThinkingController(None, FarmState(), {"mode": "adaptive"}).cfg
    anchor_obs = [ob(**steady(i)) for i in range(8)]
    drift_obs = [ob(**degrading(t)) for t in range(1, 21)]
    anchor = summarize(anchor_obs)
    series = anchor_obs[-3:] + drift_obs
    windows = _windows(series, 4)

    sliding = [max(state_distance(summarize(a), summarize(b), cfg).values()) for a, b in zip(windows, windows[1:])]
    fixed = [max(state_distance(anchor, summarize(w), cfg).values()) for w in windows]
    assert max(sliding) < 1.0, f"adjacent windows look alike: {max(sliding):.2f}"
    assert max(fixed) >= 1.0, f"fixed anchor sees the drift: {max(fixed):.2f}"
    # Every adjacent pair is similar while the distance from the anchor keeps growing.
    assert fixed[-1] > 2 * max(sliding)


# ------------------------------------------------------------ hard cadence
@pytest.mark.parametrize("deep_every", [4, 8])
def test_hard_cadence_forces_review_when_detectors_stay_quiet(tmp_path, deep_every):
    lab = Lab(tmp_path, deep_every=deep_every)
    _warm(lab, 6)
    run, longest, cadence_steps = 0, 0, 0
    # Odd step indices never hit the "periodic" signature: only the cadence can force review.
    for i in range(60):
        d = lab.step(step_index=1001 + 2 * i, **steady(i))
        if d.thinking:
            assert d.reasons == ["hard_cadence"], d.reasons
            assert d.trigger_class == "hard_cadence"
            cadence_steps += 1
            run = 0
        else:
            run += 1
            longest = max(longest, run)
    assert longest == deep_every - 1
    assert cadence_steps >= 60 // deep_every - 1


def test_detectors_cannot_extend_the_blind_interval(tmp_path):
    """Detectors reporting a perfectly healthy state change nothing about the cadence."""
    lab = Lab(tmp_path, deep_every=8)
    healthy = {"score": -1e9, "components": {}}
    lab.ctl._ood = lambda *a, **k: healthy
    lab.ctl._uncertainty = lambda *a, **k: healthy
    _warm(lab, 6)
    run = 0
    for i in range(40):
        d = lab.step(step_index=1001 + 2 * i, **steady(i))
        run = 0 if d.thinking else run + 1
        assert run <= 7
        assert d.telemetry["steps_since_validation"] <= 7
    # No configuration path lets a detector raise the interval either.
    ctl = ThinkingController(None, FarmState(), {"mode": "adaptive", "deep_every": 8,
                                                 "ood": {"threshold": 1e9}, "uncertainty": {"threshold": 1e9}})
    assert ctl.deep_every == 8
    with pytest.raises(ValueError):
        ThinkingController(None, FarmState(), {"mode": "adaptive", "deep_every": 0})


def test_failed_deep_step_does_not_reset_the_blind_interval(tmp_path):
    lab = Lab(tmp_path, deep_every=4)
    _warm(lab, 6)
    while lab.step(step_index=1001 + 2 * lab.n, **steady(lab.n)).thinking:
        pass
    seen = []
    for _ in range(12):
        d = lab.step(step_index=1001 + 2 * lab.n, malformed=True, **steady(lab.n))
        seen.append((d.thinking, d.telemetry["steps_since_validation"]))
    # Once the cadence is reached, malformed deep steps keep it reached: every step thinks.
    first_forced = next(i for i, (t, _) in enumerate(seen) if t)
    assert all(t for t, _ in seen[first_forced:])


# ------------------------------------------------------- known-event signatures
def test_known_event_signatures_still_escalate(tmp_path):
    lab = Lab(tmp_path, deep_every=1000)
    assert lab.step(**steady(0)).reasons[:2] == ["first_step", "periodic"]
    _warm(lab, 11)
    assert not lab.step(step_index=1001, **steady(1)).thinking
    lab.step(step_index=1003, malformed=True, **steady(2))
    assert "malformed_prior" in lab.step(step_index=1005, **steady(3)).reasons
    lab.step(step_index=1007, denied=1, **steady(4))
    assert "blocked_action" in lab.step(step_index=1009, **steady(5)).reasons
    lab.step(step_index=1011, tool_error=True, **steady(6))
    d = lab.step(step_index=1013, **steady(7))
    assert "tool_error" in d.reasons and d.trigger_class == "known_event"


# -------------------------------------------------------------- telemetry
def test_every_decision_records_its_evidence(tmp_path):
    lab = Lab(tmp_path)
    _warm(lab, 14)
    events = lab.store.iter_events(types=[EventType.THINKING_DECISION])
    assert len(events) == 14
    anchors = lab.store.iter_events(types=[EventType.THINKING_ANCHOR])
    assert anchors and anchors[-1].payload["anchor"]["actions"] > 0
    last = events[-1].payload
    for key in ("thinking", "reasons", "trigger_class", "steps_since_validation", "anchor_id", "anchor_version",
                "ood", "uncertainty", "drift", "deep_every"):
        assert key in last
    assert set(last["drift"]) == {"value_per_action", "conversion", "spend_efficiency", "refund_rate"}
    assert {"price", "segment", "conversion", "value_per_action"} <= set(last["ood"]["components"])
    assert {"anchor_sample", "outcome_noise", "surprise", "novel_regime", "budget_boundary"} <= \
        set(last["uncertainty"]["components"])
    assert last["anchor_id"] == anchors[-1].payload["anchor"]["anchor_id"]


def test_restart_restores_anchor_cadence_and_drift_state(tmp_path):
    lab = Lab(tmp_path, deep_every=8)
    _warm(lab, 9)
    for t in range(1, 4):
        lab.step(step_index=1001 + 2 * t, **degrading(t))
    live = lab.ctl._agents["a1"]
    live.pending_validation = None  # memory-only by design
    fresh = ThinkingController(lab.store, FarmState(), {"mode": "adaptive", "deep_every": 8})
    restored = fresh._control("a1")
    for f in ("anchor", "anchor_version", "steps_since_validation", "cusum", "validated_regimes", "last_seq"):
        assert getattr(restored, f) == getattr(live, f), f
    assert [o.to_dict() for o in restored.history] == [o.to_dict() for o in live.history]
    assert [o.to_dict() for o in restored.since_anchor] == [o.to_dict() for o in live.since_anchor]


def test_adaptive_is_opt_in_until_e13_is_promoted():
    """Flip this only together with a promotion receipt for EXPERIMENTS.md E13."""
    farm = yaml.safe_load((Path(__file__).resolve().parents[2] / "config" / "farm.yaml").read_text())
    assert farm["runtime"]["thinking"]["mode"] == "server"
    assert ThinkingController(None, FarmState(), {}).mode == "server"
