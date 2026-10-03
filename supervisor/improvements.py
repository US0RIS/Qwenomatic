"""Reversible, ledger-backed farm improvements. No population authority."""
from __future__ import annotations

import copy
import math
import random
from collections import Counter, defaultdict
from typing import Any

from storage.events import EventType, digest

FEATURES = ('knowledge', 'archive', 'crossover', 'small_model', 'autopilot',
            'crowding', 'predictions', 'adaptation', 'red_team', 'fraud', 'self_tuning')
DEFAULTS = {
    'knowledge': {'enabled': False, 'access_fraction': .5, 'max_segment_share': .6, 'min_samples': 5},
    'archive': {'enabled': False, 'max_return_share': .5, 'shift_only': True},
    'crossover': {'enabled': False, 'probability': .5},
    'small_model': {'enabled': False, 'model': 'Qwen3-4B', 'review_every': 8},
    'autopilot': {'enabled': False, 'review_every': 4, 'min_offers': 6, 'max_rate_change': .15},
    'crowding': {'enabled': False, 'weight': .15},
    'predictions': {'enabled': False},
    'adaptation': {'enabled': False, 'window_ticks': 12, 'min_samples': 20,
                   'confirm_ticks': 3, 'duration_ticks': 12, 'conversion_drop': .35,
                   'refund_increase': .15, 'exploration_share': .5, 'immigration_rate': .3},
    'red_team': {'enabled': False, 'every_ticks': 24},
    'fraud': {'enabled': False, 'min_samples': 20, 'refund_rate': .25,
              'conversion_spike': .5, 'customer_repeat_share': .5, 'reserve_fraction': .25},
    'self_tuning': {'enabled': False},
}


def settings(raw=None):
    out = copy.deepcopy(DEFAULTS)
    for name, cfg in (raw or {}).items():
        if name not in out or not isinstance(cfg, dict) or set(cfg) - set(out[name]):
            raise ValueError(f'unknown improvement setting: {name}')
        out[name].update(cfg)
    for name, cfg in out.items():
        if type(cfg['enabled']) is not bool:
            raise ValueError(f'{name}.enabled must be boolean')
        for key, value in cfg.items():
            if key in ('enabled', 'shift_only', 'model'):
                continue
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ValueError(f'{name}.{key} must be finite')
            if key.endswith(('ticks', 'samples', 'offers')) or key == 'review_every':
                if type(value) is not int or value < 1:
                    raise ValueError(f'{name}.{key} must be a positive integer')
            elif not 0 <= value <= 1:
                raise ValueError(f'{name}.{key} must be in [0,1]')
    if not .5 <= out['knowledge']['max_segment_share'] <= .6:
        raise ValueError('knowledge spread cap must be between 50% and 60%')
    if out['archive']['max_return_share'] > .5:
        raise ValueError('archive may occupy at most half of immigrant slots')
    if out['crowding']['weight'] > .25:
        raise ValueError('crowding discount must remain mild (<=25%)')
    if type(out['archive']['shift_only']) is not bool or not isinstance(out['small_model']['model'], str) or not out['small_model']['model']:
        raise ValueError('invalid archive or model setting')
    return out


def emit(sup, kind, payload, key, *, agent=None, generation=None, author='supervisor'):
    """Telemetry has its own deterministic IDs: cannot move market RNG draws."""
    return sup.store.append(kind, payload, agent_id=agent.id if agent else None,
                            lineage_id=agent.lineage_id if agent else None,
                            generation_id=generation, author=author, idempotency_key=key,
                            event_id='improvement-' + digest(key)[:32])


def offer_rows(store, *, upto_seq=None):
    return [e for e in store.iter_events(types=[EventType.OFFER_OBSERVED], upto_seq=upto_seq)
            if e.author == 'adapter:market']


def knowledge_snapshot(sup, generation):
    groups = defaultdict(list)
    for e in offer_rows(sup.store):
        if generation - 2 <= e.generation_id < generation:
            groups[e.payload['segment']].append(e)
    facts = []
    for segment, rows in sorted(groups.items()):
        n = len(rows)
        if n >= sup.improvements.cfg['knowledge']['min_samples']:
            facts.append({'segment': segment, 'samples': n,
                          'conversion_rate': sum(e.payload['converted'] for e in rows) / n,
                          'mean_price': sum(e.payload['price'] for e in rows) / n,
                          'source_seq': [e.seq for e in rows], 'generations': [max(0, generation - 2), generation - 1]})
    return {'facts': facts, 'as_of_seq': sup.store.head()[0]}


def crowd_results(results, state, generation):
    cfg = state.generations[generation].config.get('improvements', settings())['crowding']
    if not cfg['enabled']:
        return results
    counts = Counter(state.agents[a].genotype['target']['segment'] for a in results)
    for a, row in results.items():
        share = counts[state.agents[a].genotype['target']['segment']] / max(1, len(results))
        # Subtract a scale-dependent penalty, so negative fitness never improves.
        penalty = cfg['weight'] * share * abs(row['fitness']) if isinstance(row['fitness'], (int, float)) else 0
        row['crowding_share'], row['crowding_penalty'] = share, penalty
        if penalty:
            for key in ('fitness', 'fitness_lcb', 'fitness_ucb'):
                row[key] = round(row[key] - penalty, 6)
    return results


def spread_plan(plan, results, state, cfg, population_size):
    """Bound knowledge-induced convergence before reproduction, including survivors."""
    if not cfg['knowledge']['enabled']:
        return plan
    cap = max(1, math.floor(cfg['knowledge']['max_segment_share'] * population_size))
    counts = Counter(state.agents[a].genotype['target']['segment'] for a in plan['survivors'])
    for segment, count in sorted(counts.items()):
        excess = max(0, count - cap)
        candidates = sorted([a for a in plan['survivors'] if state.agents[a].genotype['target']['segment'] == segment],
                            key=lambda a: (results[a]['fitness_ucb'] or 0, a))
        for a in candidates[:excess]:
            plan['survivors'].remove(a)
            if a in plan['elites']:
                plan['elites'].remove(a)
            plan['retirements'].append({'agent_id': a, 'reason': 'knowledge segment spread cap'})
            plan['offspring'].append({'slot': len(plan['offspring']), 'origin': 'immigrant', 'parent_id': None,
                                     'reason': 'knowledge spread'})
    plan['segment_cap'] = cap
    return plan


class Improvements:
    def __init__(self, sup):
        self.sup = sup
        self.cfg = settings(sup.config.farm.get('improvements'))

    def generation_cfg(self):
        return self.sup.generation_config().get('improvements', self.cfg)

    def start(self, generation, population):
        cfg = self.generation_cfg()
        for name in ('knowledge', 'small_model', 'autopilot', 'predictions'):
            if not cfg[name]['enabled']:
                continue
            ids = sorted(population)
            rng = random.Random(int(digest([self.sup.config.seed, generation, name, 'assignment'])[:16], 16))
            treated = rng.sample(ids, round(len(ids) * (cfg[name].get('access_fraction', .5))))
            emit(self.sup, EventType.FEATURE_ASSIGNMENT, {'feature': name, 'treatment': sorted(treated),
                 'control': sorted(set(ids) - set(treated))}, f'feature:{generation}:{name}', generation=generation)
        if cfg['knowledge']['enabled']:
            emit(self.sup, EventType.KNOWLEDGE_SNAPSHOT, knowledge_snapshot(self.sup, generation),
                 f'knowledge:{generation}', generation=generation)

    def treated(self, name, agent_id):
        g = self.sup.state.current_generation
        e = self.sup.store.get_by_idempotency(f'feature:{g}:{name}')
        return bool(e and agent_id in e.payload['treatment'])

    def knowledge(self, agent):
        if not self.treated('knowledge', agent.id):
            return []
        e = self.sup.store.get_by_idempotency(f'knowledge:{self.sup.state.current_generation}')
        return copy.deepcopy(e.payload['facts']) if e else []

    def route(self, agent, st):
        cfg = self.generation_cfg()
        if not (cfg['autopilot']['enabled'] or cfg['small_model']['enabled']):
            return 'primary', None, None
        rows = [e for e in offer_rows(self.sup.store) if e.agent_id == agent.id]
        reasons = []
        minimum = cfg['autopilot']['min_offers']
        recent = rows[-minimum:]
        g = self.sup.state.current_generation
        c = self.sup.state.counter(g, agent.id)
        if len(recent) < minimum:
            reasons.append('insufficient_outcomes')
        if c.net_realized <= 0 or c.refunds or c.denied or c.malformed or c.jobs_failed or c.violations:
            reasons.append('unprofitable_or_unhealthy')
        keys = {(e.payload['segment'], e.payload['price'], e.payload.get('tactic', 'standard')) for e in recent}
        if len(keys) != 1:
            reasons.append('strategy_changed')
        if recent:
            half = max(1, len(recent) // 2)
            rate1 = sum(e.payload['converted'] for e in recent[:half]) / half
            rate2 = sum(e.payload['converted'] for e in recent[half:]) / max(1, len(recent) - half)
            if abs(rate1 - rate2) > cfg['autopilot']['max_rate_change']:
                reasons.append('outcome_drift')
        if self.shift_active():
            reasons.append('market_shift')
        if self.payout_held(agent.lineage_id):
            reasons.append('anomaly_hold')
        forecast = None
        if cfg['predictions']['enabled'] and self.treated('predictions', agent.id):
            predictions = self.sup.store.iter_events(types=[EventType.PREDICTION_SCORED], agent_id=agent.id)
            if predictions:
                forecast = predictions[-1].payload['probability']
            else:
                reasons.append('no_valid_forecast')
        idx = st['step_index']
        # Hard cadence and last-result checks cannot be suppressed by model claims.
        mem = st.get('memory', [])
        if not mem or mem[-1].get('status') != 'ok' or mem[-1].get('tool') != 'market.offer':
            reasons.append('no_repeatable_action')
        mode = 'primary'
        action = None
        primary_review = ((cfg['autopilot']['enabled'] and idx % cfg['autopilot']['review_every'] == 0)
                          or (cfg['small_model']['enabled'] and idx % cfg['small_model']['review_every'] == 0))
        if not reasons and not primary_review:
            if cfg['autopilot']['enabled'] and self.treated('autopilot', agent.id) and idx % cfg['autopilot']['review_every']:
                mode = 'autopilot'
                last = recent[-1].payload
                action = {'tool': 'market.offer', 'args': {'segment': last['segment'], 'price': last['price'],
                                                         'tactic': last.get('tactic', 'standard')}}
                if forecast is not None:
                    action['prediction'] = forecast
            elif cfg['small_model']['enabled'] and self.treated('small_model', agent.id) and idx % cfg['small_model']['review_every']:
                mode = 'small'
        if not (cfg['autopilot']['enabled'] or cfg['small_model']['enabled']):
            return 'primary', None, recent[-1].payload if recent else None
        emit(self.sup, EventType.ROUTING_DECISION, {'mode': mode, 'reasons': reasons or ['stable_profitable'],
             'step_index': idx, 'review_interval': cfg['autopilot' if mode == 'autopilot' else 'small_model']['review_every']},
             f'route:{g}:{agent.id}:{idx}', agent=agent, generation=g)
        return mode, action, recent[-1].payload if recent else None

    def shift_active(self):
        rows = self.sup.store.iter_events(types=[EventType.MARKET_SHIFT])
        return bool(rows and rows[-1].payload['until_tick'] > self.sup.clock.tick)

    def observe_tick(self, generation, tick):
        cfg = self.generation_cfg()
        if cfg['adaptation']['enabled']:
            self.detect_shift(generation, tick)
        if cfg['fraud']['enabled']:
            self.fraud_scan(generation, tick)
        if cfg['red_team']['enabled'] and tick % cfg['red_team']['every_ticks'] == 0:
            from .red_team import run_red_team
            run_red_team(self.sup, tick)

    def detect_shift(self, g, tick):
        cfg = self.generation_cfg()['adaptation']
        w = cfg['window_ticks']
        rows = offer_rows(self.sup.store)
        previous = [e for e in rows if tick - 2*w < e.payload['tick'] <= tick - w]
        recent = [e for e in rows if tick - w < e.payload['tick'] <= tick]
        rate = lambda rs: sum(e.payload['converted'] for e in rs) / max(1, len(rs))
        # Monetary refund ratios use settled adapter evidence, not model text.
        financial = self.sup.store.iter_events(types=[EventType.FINANCIAL_EVENT])
        def refund_ratio(low, high):
            fs = [e.payload for e in financial if low < e.payload.get('tick', -1) <= high and e.payload.get('status') == 'realized']
            gross = sum(p['amount'] for p in fs if p['category'] == 'gross_revenue')
            return sum(p['amount'] for p in fs if p['category'] == 'refund') / max(1, gross)
        enough = min(len(previous), len(recent)) >= cfg['min_samples']
        signal = enough and ((rate(previous) > 0 and rate(recent) < rate(previous) * (1 - cfg['conversion_drop']))
                            or refund_ratio(tick-w, tick) - refund_ratio(tick-2*w, tick-w) > cfg['refund_increase'])
        prior = self.sup.store.get_by_idempotency(f'regime:{tick-1}')
        streak = (prior.payload['streak'] if prior else 0) + 1 if signal else 0
        emit(self.sup, EventType.REGIME_OBSERVATION, {'tick': tick, 'streak': streak, 'signal': signal,
             'baseline_samples': len(previous), 'recent_samples': len(recent),
             'baseline_rate': rate(previous), 'recent_rate': rate(recent)}, f'regime:{tick}', generation=g)
        if streak >= cfg['confirm_ticks'] and not self.shift_active():
            emit(self.sup, EventType.MARKET_SHIFT, {'tick': tick, 'until_tick': tick + cfg['duration_ticks'],
                 'exploration_share': cfg['exploration_share'], 'immigration_rate': cfg['immigration_rate'],
                 'reason': 'sustained conversion decline or refund spike'}, f'shift:{tick}', generation=g)

    def archive(self, agent, generation, reason):
        if not self.generation_cfg()['archive']['enabled']:
            return
        c = self.sup.state.counter(generation, agent.id)
        rows = [e for e in offer_rows(self.sup.store) if e.agent_id == agent.id]
        # No self-authored memories, claims or free-form text are admitted as evidence.
        emit(self.sup, EventType.RETIREMENT_REPORT, {'reason': reason, 'genotype': agent.genotype,
             'genotype_digest': digest(agent.genotype), 'counters': c.to_dict(), 'source_seq': [e.seq for e in rows],
             'safe_to_return': not bool(c.violations) and not reason.startswith(('disqualified', 'health'))},
             f'archive:{generation}:{agent.id}', agent=agent, generation=generation)

    def fraud_scan(self, g, tick):
        cfg = self.generation_cfg()['fraud']
        grouped = defaultdict(list)
        for e in offer_rows(self.sup.store):
            grouped[e.lineage_id].append(e)
        previous_flags = self.sup.store.iter_events(types=[EventType.ANOMALY_FLAG])
        reviewed = {e.payload['flag_id'] for e in self.sup.store.iter_events(types=[EventType.ANOMALY_REVIEW])}
        active = {e.payload['lineage_id'] for e in previous_flags if e.event_id not in reviewed}
        for lin in sorted({a.lineage_id for a in self.sup.state.agents.values()}):
            rows = grouped[lin]
            cs = [c for gs in self.sup.state.counters.values() for a, c in gs.items()
                  if self.sup.state.agents[a].lineage_id == lin]
            gross = sum(c.gross_revenue for c in cs)
            refunds = sum(c.refunds for c in cs)
            reasons = []
            evidence_digest = digest([len(rows), gross, refunds, [e.seq for e in rows]])
            prior_reviewed = [e for e in previous_flags if e.event_id in reviewed
                              and e.payload['lineage_id'] == lin and e.payload.get('evidence_digest') == evidence_digest]
            if len(rows) >= cfg['min_samples']:
                if refunds / max(1, gross) > cfg['refund_rate']:
                    reasons.append('refund_rate')
                half = len(rows) // 2
                rates = [sum(e.payload['converted'] for e in rs) / len(rs) for rs in (rows[:half], rows[half:])]
                if rates[1] - rates[0] > cfg['conversion_spike']:
                    reasons.append('conversion_spike')
                customers = [e.payload['customer_hash'] for e in rows if e.payload.get('customer_hash')]
                if len(customers) >= cfg['min_samples'] and max(Counter(customers).values()) / len(customers) > cfg['customer_repeat_share']:
                    reasons.append('customer_concentration')
            if reasons and lin not in active and not prior_reviewed:
                e = emit(self.sup, EventType.ANOMALY_FLAG, {'lineage_id': lin, 'reasons': reasons, 'tick': tick, 'evidence_digest': evidence_digest,
                         'gross': gross, 'refunds': refunds, 'samples': len(rows), 'source_seq': [e.seq for e in rows]},
                         f'anomaly:{lin}:{tick}', generation=g)
                # Lineage is explicit even when no currently active agent represents it.
                # emit's agent-free event uses payload lineage for the hold query below.
                # Persist immutable flag identity separately from scoring.
                active.add(lin)
                emit(self.sup, EventType.RESERVE_UPDATED, {'lineage_id': lin, 'flag_id': e.event_id,
                     'held': True, 'reserved': max(0, gross * cfg['reserve_fraction']),
                     'clawback': min(refunds, gross * cfg['reserve_fraction']), 'tick': tick},
                     f'reserve-hold:{lin}:{tick}', generation=g)
            reserved = max(0, gross * cfg['reserve_fraction'] - refunds)
            emit(self.sup, EventType.RESERVE_UPDATED, {'lineage_id': lin, 'held': self.payout_held(lin),
                 'reserved': reserved, 'clawback': min(refunds, gross * cfg['reserve_fraction']),
                 'distributable': max(0, sum(c.net_realized for c in cs) - reserved), 'tick': tick},
                 f'reserve:{lin}:{tick}', generation=g)

    def payout_held(self, lineage_id):
        holds = self.sup.store.iter_events(types=[EventType.RESERVE_UPDATED])
        reviewed = {e.payload['flag_id'] for e in self.sup.store.iter_events(types=[EventType.ANOMALY_REVIEW])}
        return any(e.payload.get('lineage_id') == lineage_id and e.payload.get('flag_id') not in reviewed
                   for e in holds if e.payload.get('flag_id'))

    def review_anomaly(self, flag_id, operator, note):
        flags = self.sup.store.iter_events(types=[EventType.ANOMALY_FLAG])
        if not any(e.event_id == flag_id for e in flags) or not note.strip() or not operator:
            raise ValueError('known flag, operator and review note required')
        emit(self.sup, EventType.ANOMALY_REVIEW, {'flag_id': flag_id, 'operator': operator, 'note': note},
             f'anomaly-review:{flag_id}', author='operator:' + operator)
