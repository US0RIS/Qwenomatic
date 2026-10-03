"""Randomized paired feature campaigns, using the existing generation summaries."""
from __future__ import annotations
import copy
import math
import random
from pathlib import Path

from storage.events import EventType, digest
from supervisor.config import FarmConfig
from supervisor.improvements import FEATURES, settings, offer_rows
from supervisor.experiments.evolution_ab import summarize
from supervisor.audit import verify


def prediction_report(sup):
    scored = sup.store.iter_events(types=[EventType.PREDICTION_SCORED])
    by_agent_gen = {}
    for e in scored:
        by_agent_gen.setdefault((e.agent_id, e.generation_id), []).append(e.payload['brier_error'])
    pairs = []
    for (a, g), errors in sorted(by_agent_gen.items()):
        next_counter = sup.state.counters.get(g+1, {}).get(a)
        if next_counter and next_counter.steps:
            pairs.append({'agent_id': a, 'generation': g, 'mean_brier': sum(errors)/len(errors),
                          'next_generation_net_per_step': next_counter.net_realized / next_counter.steps})
    correlation = None
    if len(pairs) >= 3:
        xs, ys = [r['mean_brier'] for r in pairs], [r['next_generation_net_per_step'] for r in pairs]
        mx, my = sum(xs)/len(xs), sum(ys)/len(ys)
        denom = math.sqrt(sum((x-mx)**2 for x in xs) * sum((y-my)**2 for y in ys))
        correlation = sum((x-mx)*(y-my) for x,y in zip(xs,ys))/denom if denom else None
    return {'scored_offers': len(scored), 'mean_brier': sum(e.payload['brier_error'] for e in scored)/len(scored) if scored else None,
            'trust': {a: {'samples': len(errs), 'accuracy_trust': 1-sum(errs)/len(errs)}
                      for (a,g),errs in by_agent_gen.items() if g == (sup.state.current_generation or 1)-1},
            'next_generation_pairs': pairs, 'brier_next_profit_correlation': correlation,
            'trust_effect': 'observational evidence only; never authority or revenue'}


def run_campaign(root, feature, *, seeds=(101, 202), generations=3, config_dir=None, changes=None,
                 overrides=None):
    from supervisor.core import Supervisor
    from supervisor.tuning import apply_changes
    if feature not in FEATURES or len(set(seeds)) < 2 or generations < 2:
        raise ValueError('known feature, two distinct seeds and at least two generations required')
    if feature == 'self_tuning' and not changes:
        raise ValueError('self-tuning needs a bounded candidate')
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    source = FarmConfig.load(config_dir, overrides=overrides)
    baseline_hash = digest(source.farm)
    from supervisor.roles import layout
    business_size = layout(source.farm)['counts']['business']
    pairs = []
    for index, seed in enumerate(seeds):
        order = ['control', 'treatment']
        random.Random(seed).shuffle(order)
        arms = {}
        for arm in order:
            data = root / f'pair-{index+1}' / arm
            if (data / 'ledger.sqlite3').exists():
                raise ValueError('campaign output already contains a ledger; choose a fresh path')
            cfg = copy.deepcopy(source)
            cfg.data_dir = data
            cfg.farm['farm']['seed'] = seed
            # Disposable simulations never inherit live routes, credentials or providers.
            cfg.farm['inference']['backend'] = 'simulated'
            cfg.farm['clock']['mode'] = 'simulated'
            # Economic paired campaigns hold the business population fixed and
            # never recursively start specialist campaigns or red-team work.
            cfg.farm.setdefault('roles', {})['enabled'] = False
            cfg.farm['farm']['population_size'] = business_size
            cfg.farm['improvements'] = settings()
            if arm == 'treatment':
                if feature == 'self_tuning':
                    apply_changes(cfg.farm, changes)
                else:
                    cfg.farm['improvements'][feature]['enabled'] = True
                    if feature == 'archive':
                        cfg.farm['improvements']['archive']['shift_only'] = False
            if feature == 'adaptation':
                cfg.farm['simulation']['market']['regimes'] = [{'after_hours': 6, 'segments': {
                    s: {'conversion_multiplier': .1, 'refund_rate': .4} for s in cfg.segments}}]
            sup = Supervisor(cfg)
            try:
                sup.run(generations=generations)
                checks = verify(sup)
                if not (checks['hash_chain']['ok'] and checks['accounting']['ok']
                        and all(checks['selection_replay'].values()) and checks['generations_started_once']):
                    raise RuntimeError('campaign replay failed')
                arms[arm] = {'summary': summarize(data, arm, generations).to_dict(), 'audit': checks,
                             'predictions': prediction_report(sup),
                             'archive_returns': sum(a.origin == 'archive_return' for a in sup.state.agents.values()),
                             'routing': dict(__import__('collections').Counter(e.payload['mode'] for e in sup.store.iter_events(types=[EventType.ROUTING_DECISION]))),
                             'shifts': len(sup.store.iter_events(types=[EventType.MARKET_SHIFT])),
                             'anomaly_flags': len(sup.store.iter_events(types=[EventType.ANOMALY_FLAG])),
                             'red_team_confirmed': sum(e.payload['confirmed_loophole'] for e in sup.store.iter_events(types=[EventType.RED_TEAM_FINDING])),
                             'violations': sup.state.violations_total}
            finally:
                sup.close()
        t, c = arms['treatment']['summary']['total'], arms['control']['summary']['total']
        pairs.append({'seed': seed, 'order': order, 'arms': arms,
                      'delta_net': t['net_realized']-c['net_realized'],
                      'delta_profit_per_gpu_hour': t['profit_per_gpu_hour']-c['profit_per_gpu_hour']
                          if t['profit_per_gpu_hour'] is not None and c['profit_per_gpu_hour'] is not None else None})
    return {'feature': feature, 'changes': changes, 'seeds': list(seeds), 'generations': generations,
            'baseline_hash': baseline_hash, 'status': 'simulation_verified', 'promoted': False,
            'population_scope': 'business-only paired simulations; specialist overhead excluded',
            'business_population_size': business_size,
            'pairs': pairs, 'mean_delta_net': sum(p['delta_net'] for p in pairs)/len(pairs),
            'limits': 'Simulation mechanism verification only. GPU time is emulated; no real revenue or promotion.'}
