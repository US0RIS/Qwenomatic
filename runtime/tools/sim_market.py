"""Deterministic simulated economy for Phase 0 (DESIGN §16).

This is the "external world": the supervisor's trusted adapters observe it,
agents only act on it through tools. Every random draw is keyed by a stable
identifier, so outcomes do not depend on thread scheduling or call order.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from storage.events.canonical import digest


@dataclass(frozen=True)
class SegmentParams:
    name: str
    base_conversion: float
    reference_price: float
    ad_cost: float
    fee_rate: float
    fee_fixed: float
    settlement_delay_hours: float
    refund_rate: float
    refund_delay_hours: float
    decay: dict[str, float] | None = None

    @classmethod
    def from_dict(cls, name: str, d: dict[str, Any]) -> "SegmentParams":
        return cls(
            name=name, base_conversion=float(d["base_conversion"]), reference_price=float(d["reference_price"]),
            ad_cost=float(d.get("ad_cost", 0.0)), fee_rate=float(d.get("fee_rate", 0.0)),
            fee_fixed=float(d.get("fee_fixed", 0.0)),
            settlement_delay_hours=float(d.get("settlement_delay_hours", 0.0)),
            refund_rate=float(d.get("refund_rate", 0.0)), refund_delay_hours=float(d.get("refund_delay_hours", 0.0)),
            decay=d.get("decay"),
        )


@dataclass(frozen=True)
class OfferOutcome:
    opportunity_id: str
    segment: str
    price: float
    converted: bool
    fee: float
    settle_at: datetime
    refund: bool
    refund_delay_hours: float
    conversion_probability: float
    details: dict[str, Any] | None = None


class SimulatedMarket:
    model = "toy_v1"
    def __init__(self, config: dict[str, Any], seed: int, start: datetime) -> None:
        self.seed = seed
        self.start = start
        self.regimes = sorted(config.get('regimes', []), key=lambda r: r['after_hours'])
        self.survey_noise = float(config.get("survey_noise", 0.25))
        self.segments = {n: SegmentParams.from_dict(n, d) for n, d in config["segments"].items()}
        # Test hook: multiplies conversion for offers using a named tactic. The
        # policy gateway must stop forbidden tactics before they ever get here.
        self.tactic_multipliers: dict[str, float] = dict(config.get("tactic_multipliers", {}))
        self.offers_by_tactic: dict[str, int] = {}

    def _rng(self, *key: object) -> random.Random:
        return random.Random(int(digest([self.seed, *key])[:16], 16))

    def decay_factor(self, seg: SegmentParams, now: datetime) -> float:
        if not seg.decay:
            return 1.0
        hours = max(0.0, (now - self.start).total_seconds() / 3600)
        half_life = float(seg.decay["half_life_hours"])
        return max(float(seg.decay.get("floor", 0.0)), 0.5 ** (hours / half_life))

    def regime_values(self, segment, now):
        hours = max(0, (now - self.start).total_seconds() / 3600)
        values = {'conversion_multiplier': 1., 'refund_rate': self.segments[segment].refund_rate}
        for regime in self.regimes:
            if hours >= regime['after_hours']:
                values.update(regime.get('segments', {}).get(segment, {}))
        return values

    def conversion_probability(self, segment: str, price: float, now: datetime, tactic: str | None = None) -> float:
        seg = self.segments[segment]
        p = seg.base_conversion * math.exp(-price / seg.reference_price) * self.decay_factor(seg, now)
        p *= self.regime_values(segment, now)['conversion_multiplier']
        if tactic:
            p *= self.tactic_multipliers.get(tactic, 1.0)
        return min(max(p, 0.0), 0.99)

    def expected_profit_per_offer(self, segment: str, price: float, now: datetime) -> float:
        seg = self.segments[segment]
        p = self.conversion_probability(segment, price, now)
        fee = seg.fee_rate * price + seg.fee_fixed
        return p * (price * (1 - self.regime_values(segment, now)['refund_rate']) - fee) - seg.ad_cost

    def resolve_offer(
        self, opportunity_id: str, segment: str, price: float, now: datetime, tactic: str | None = None
    ) -> OfferOutcome:
        seg = self.segments[segment]
        if tactic:
            self.offers_by_tactic[tactic] = self.offers_by_tactic.get(tactic, 0) + 1
        rng = self._rng("offer", opportunity_id)
        prob = self.conversion_probability(segment, price, now, tactic)
        converted = rng.random() < prob
        refund = converted and rng.random() < self.regime_values(segment, now)['refund_rate']
        return OfferOutcome(
            opportunity_id=opportunity_id, segment=segment, price=round(price, 2), converted=converted,
            fee=round(seg.fee_rate * price + seg.fee_fixed, 6) if converted else 0.0,
            settle_at=now + timedelta(hours=seg.settlement_delay_hours), refund=refund,
            refund_delay_hours=seg.refund_delay_hours, conversion_probability=prob,
        )

    def survey(self, segment: str, now: datetime, query_id: str) -> dict[str, Any]:
        seg = self.segments[segment]
        rng = self._rng("survey", query_id)
        noise = lambda: math.exp(rng.gauss(0.0, self.survey_noise))  # noqa: E731
        return {
            "segment": segment,
            "typical_price": round(seg.reference_price * noise(), 2),
            "interest_level": round(min(1.0, seg.base_conversion * self.decay_factor(seg, now) * noise()), 3),
            "acquisition_cost": round(seg.ad_cost * noise(), 2),
            "payment_delay_hours": seg.settlement_delay_hours,
        }


def build_market(config, seed, start, store):
    """Freeze market assumptions for a ledger; never silently change its world."""
    from storage.events import EventType
    if store.iter_events(types=[EventType.MARKET_RUN_FINISHED], limit=1):
        raise ValueError('completed market campaign ledger is read-only; use a fresh --data-dir')
    model = config.get('model', 'toy_v1')
    if model not in ('toy_v1', 'constrained_v1'):
        raise ValueError(f'unknown market model: {model}')
    identity = digest({'config': config, 'seed': seed, 'start': start.isoformat()})
    previous = store.iter_events(types=[EventType.MARKET_CONFIGURED], limit=1)
    if previous and previous[0].payload['identity'] != identity:
        raise ValueError('market assumptions changed; use a fresh --data-dir for this experiment')
    if not previous and model != 'toy_v1' and store.iter_events(types=[EventType.GENERATION_STARTED], limit=1):
        raise ValueError('legacy ledger: use a fresh --data-dir for the constrained market')
    if model == 'constrained_v1':
        from .constrained_market import ConstrainedMarket
        market = ConstrainedMarket(config, seed, start, store)
    else:
        market = SimulatedMarket(config, seed, start)
    store.append(EventType.MARKET_CONFIGURED,
                 {'model': model, 'identity': identity, 'config': config,
                  'evidence_status': 'assumptions; real-world validity unestablished'},
                 idempotency_key='market-configured')
    return market
