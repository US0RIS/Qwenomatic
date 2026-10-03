"""Predeclared paired stress experiments, including the complete payment tail.

Worlds are sensitivity scenarios, not samples from an estimated real-world
distribution. Confidence intervals measure Monte Carlo error conditional on
the assumptions. They cannot establish business viability.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import random
from statistics import mean

from storage.events import EventType, digest
from supervisor.accounting import Attribution, Ledger
from supervisor.audit import verify
from supervisor.config import FarmConfig
from supervisor.core import Supervisor


SCENARIOS = {
    'baseline': {},
    'demand_slump': {'demand_multiplier': .5},
    'competition': {'competition_multiplier': 2.},
    'acquisition_shock': {'cost_multiplier': 2., 'demand_multiplier': .8},
    'delivery_stress': {'quality_multiplier': .7, 'delay_multiplier': 2., 'cost_multiplier': 1.5},
}


def source_inventory():
    root = Path(__file__).resolve().parents[2]
    paths = [root/'pyproject.toml']
    for directory in ('runtime', 'supervisor', 'storage', 'scripts', 'deploy', 'config', 'dashboard'):
        paths.extend(p for p in (root/directory).rglob('*')
                     if p.is_file() and p.suffix in ('.py', '.yaml', '.sql', '.html'))
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(paths)}


def audit_ok(report):
    return (report['hash_chain']['ok'] and report['accounting']['ok']
            and all(report['selection_replay'].values()) and report['generations_started_once'])


def settle_tail(sup):
    """No new sales; settle all liabilities and charge capital tied in receivables.

All delivery/support labor was expensed when the order was accepted. Operating
overhead covers the active campaign window; this liquidation tail charges only
receivables financing. No waiting in real time and no extra evolutionary steps.
"""
    current = sup.clock.now_dt()
    with sup.store.transaction():
        sup._market_financing(current, sup.state.current_generation, sup.clock.tick)
    count = 0
    while sup.state.pending_settlements:
        due = [datetime.fromisoformat(p['due_at']) for p in sup.state.pending_settlements.values() if p.get('source') == 'payments']
        if not due:
            raise RuntimeError('unhandled liability source in campaign')
        nxt = max(current, min(due))
        sup.clock.set_tick(max(sup.clock.tick, math.ceil((nxt-sup.clock.start).total_seconds()/sup.config.tick_seconds)))
        nxt = sup.clock.now_dt()
        days = (nxt-current).total_seconds()/86400
        with sup.store.transaction():
            for p in list(sup.state.pending_settlements.values()):
                if p.get('kind') != 'sale':
                    continue
                amount = p['amount'] * sup.market.settings['annual_capital_rate'] * days/365
                attr = Attribution(p['agent_id'], p['lineage_id'], sup.state.current_generation,
                                   p.get('step_id'), sup.clock.tick, nxt)
                sup.market_costs.charge(attr, f"financing:{count}:{p['external_reference']}", {'receivables_financing': amount},
                                       step_generation=p.get('step_generation'))
            settled = sup.payments.reconcile(nxt, sup.state.current_generation, sup.clock.tick)
        if not settled:
            raise RuntimeError('payment tail made no progress')
        current = nxt
        count += 1
    return {'reconciliation_batches': count, 'paid_through': current.isoformat(), 'pending': 0}


def arm(cfg, generations, name, progress=print):
    sup = Supervisor(cfg)
    try:
        sup.bootstrap()
        initial = {a.id: digest(a.genotype) for a in sup.state.active_agents()}
        for g in range(generations):
            sup.run(generations=1)
            if sup.state.halted or sup.state.current_generation != g+1:
                raise RuntimeError(f'{name} halted before generation {g} completed')
            progress(f'{name}: generation {g+1}/{generations} closed', flush=True)
        if name == 'control':
            current = {a.id: digest(a.genotype) for a in sup.state.active_agents()}
            if current != initial:
                raise RuntimeError('control population changed; pair invalid')
        before = Ledger.pnl(sup.store.iter_events())
        tail = settle_tail(sup)
        sup.store.append(EventType.MARKET_RUN_FINISHED, {'paid_through': tail['paid_through'],
            'reason': 'payment/refund runoff completed; no further sales are permitted'},
            idempotency_key='market-run-finished')
        audit = verify(sup)
        if not audit_ok(audit):
            raise RuntimeError(f'{name} ledger audit failed')
        all_events = sup.store.iter_events()
        financial = [e for e in all_events if e.type is EventType.FINANCIAL_EVENT]
        pnl = Ledger.pnl(financial)
        after_baseline = [e for e in financial if e.payload.get('step_generation', e.generation_id) != 0]
        baseline = [e for e in financial if e.payload.get('step_generation', e.generation_id) == 0]
        offers = [e for e in all_events if e.type is EventType.OFFER_OBSERVED]
        source_costs = Counter()
        for event in financial:
            p = event.payload
            if p['status'] == 'realized' and p['type'] in ('expense', 'fee'):
                source_costs[p.get('kind', p['category'])] += p['amount']
        counters = [c for generation in sup.state.counters.values() for c in generation.values()]
        usage = [e.payload.get('usage', {}) for e in all_events if e.type is EventType.INFERENCE_JOB_COMPLETED]
        total_steps = sum(c.steps for c in counters)
        lineage_steps = Counter()
        for generation in sup.state.counters.values():
            for agent_id, c in generation.items():
                lineage_steps[sup.state.agents[agent_id].lineage_id] += c.steps
        guardrails = {'policy_violations': sup.state.violations_total,
            'human_interventions': sup.state.human_interventions,
            'malformed': sum(c.malformed for c in counters), 'steps': total_steps,
            'failed_tool_calls': sum(e.type is EventType.TOOL_INVOKED and not e.payload['ok'] for e in all_events),
            'failed_inference': sum(e.type is EventType.INFERENCE_JOB_FAILED for e in all_events),
            'step_exceptions': sum(e.type is EventType.HEALTH_EVENT and e.payload.get('kind') in ('tool_exception', 'step_exception') for e in all_events),
            'gpu_seconds': sum(c.gpu_seconds for c in counters),
            'prompt_tokens': sum(u.get('prompt_tokens', 0) for u in usage),
            'completion_tokens': sum(u.get('completion_tokens', 0) for u in usage),
            'external_spend': pnl['external_spend'], 'refunds': pnl['refunds'],
            'max_lineage_call_share': max(lineage_steps.values(), default=0)/max(1, total_steps)}
        guardrails['malformed_rate'] = guardrails['malformed']/max(1, total_steps)
        return {'arm': name, 'ledger': str(cfg.data_dir/'ledger.sqlite3'),
                'model': sup.backend.model, 'backend': sup.backend.name,
                'role_counts': dict(Counter(a.role for a in sup.state.active_agents())),
                'initial_population_digest': digest(initial), 'pnl_before_tail': before,
                'pnl_after_tail': pnl, 'post_baseline_net': Ledger.pnl(after_baseline)['net_realized_profit'],
                'baseline_net': Ledger.pnl(baseline)['net_realized_profit'],
                'costs': {k: round(v, 6) for k, v in source_costs.items()},
                'guardrails': guardrails,
                'net_per_model_step': pnl['net_realized_profit']/max(1, total_steps),
                'net_per_gpu_hour': pnl['net_realized_profit']/max(1e-9, guardrails['gpu_seconds']/3600),
                'net_per_external_dollar': pnl['net_realized_profit']/max(1e-9, pnl['external_spend']),
                'offers': len(offers), 'orders': sum(e.payload['converted'] for e in offers),
                'collected_orders': sum(e.payload['converted'] and not e.payload['market_details']['payment_failed'] for e in offers),
                'minimum_cash': round(sup.market.minimum_cash, 6), 'ending_cash': round(sup.market.cash, 6),
                'capital_exhausted': sup.market.minimum_cash <= 0,
                'tail': tail, 'audit': audit}
    finally:
        sup.close()


def interval(values, seed=193, draws=4000):
    if len(values) < 2:
        return None
    rng = random.Random(seed)
    samples = sorted(mean(rng.choices(values, k=len(values))) for _ in range(draws))
    return [samples[int(draws*.025)], samples[min(draws-1, int(draws*.975))]]


def summarize(pairs):
    result = {}
    for scenario in sorted({p['scenario'] for p in pairs}):
        rows = [p for p in pairs if p['scenario'] == scenario]
        deltas = [p['delta_post_baseline_net'] for p in rows]
        profits = [p['treatment']['pnl_after_tail']['net_realized_profit'] for p in rows]
        result[scenario] = {'independent_seed_pairs': len(rows), 'mean_delta': mean(deltas),
            'delta_bootstrap_95': interval(deltas), 'mean_net_after_tail': mean(profits),
            'profit_bootstrap_95': interval(profits), 'profitable_runs': sum(x > 0 for x in profits),
            'loss_fraction': sum(x < 0 for x in profits)/len(profits),
            'worst_net': min(profits), 'best_net': max(profits),
            'capital_exhausted_runs': sum(p['treatment']['capital_exhausted'] for p in rows),
            'guardrails_pass': all(p['guardrails_pass'] for p in rows)}
    adequate = all(r['independent_seed_pairs'] >= 10 for r in result.values())
    positive = adequate and all(r['delta_bootstrap_95'][0] > 0 and r['profit_bootstrap_95'][0] > 0
                                and not r['capital_exhausted_runs'] and r['guardrails_pass'] for r in result.values())
    return {'scenarios': result,
            'simulation_screen': 'promising_under_tested_assumptions' if positive else 'inconclusive_or_fails_assumptions',
            'enough_seeds_for_screen': adequate, 'real_world_validity': 'unestablished',
            'interpretation': 'Intervals measure seed variation conditional on these assumptions, not real-world forecast accuracy.',
            'missing_evidence': ['Independently verified customer purchases and mature refund outcomes.',
                'Measured quality and delivery cost for actual Qwen-generated work.',
                'Prospective comparison with a simple non-agent workflow on the same customers.']}


def run_campaign(config_dir, root, *, seeds=(101, 202, 303), generations=7,
                 scenarios=tuple(SCENARIOS), overrides=None, progress=print):
    if generations < 2 or not seeds or len(set(seeds)) != len(seeds):
        raise ValueError('at least two generations and distinct seeds are required')
    if not scenarios or len(set(scenarios)) != len(scenarios) or any(s not in SCENARIOS for s in scenarios):
        raise ValueError('select distinct, known market scenarios')
    root = Path(root)
    root.mkdir(parents=True, exist_ok=False)
    template = FarmConfig.load(config_dir, overrides=overrides)
    if template.farm['simulation']['market'].get('model') != 'constrained_v1':
        raise ValueError('market validity campaign requires constrained_v1')
    if template.farm['clock']['mode'] != 'simulated':
        raise ValueError('validity campaign requires a simulated clock, including with real inference')
    # The complete, fixed plan exists before the first result. All seeds and
    # scenarios, including losing trials, are retained in the final report.
    plan = {'schema_version': 1, 'seeds': list(seeds), 'generations': generations,
            'scenarios': {s: SCENARIOS[s] for s in scenarios}, 'config_hashes': template.hashes(),
            'source_sha256': source_inventory(),
            'backend': template.farm['inference']['backend'],
            'primary_outcome': 'post-baseline net after all payments and refunds; treatment minus frozen control',
            'secondary_outcome': 'total net vs doing nothing (zero incremental net)',
            'minimum_seeds_for_screen': 10, 'real_world_validity': 'unestablished',
            'guardrails': {'policy_violations': 0, 'step_exceptions': 0,
                          'max_relative_call_imbalance': .05, 'max_malformed_rate_increase': .01}}
    (root/'plan.json').write_text(json.dumps(plan, indent=2)+'\n', encoding='utf-8')
    (root/'incomplete.json').write_text(json.dumps({'status': 'running or interrupted; no validity claim',
        'plan_digest': digest(plan)}, indent=2)+'\n', encoding='utf-8')
    pairs = []
    try:
        for scenario in scenarios:
            for index, seed in enumerate(seeds):
                progress(f'World {scenario}, seed {seed}', flush=True)
                arms = {}
                for name in (('treatment', 'control') if index % 2 == 0 else ('control', 'treatment')):
                    cfg = deepcopy(template)
                    cfg.data_dir = root/scenario/str(seed)/name
                    cfg.farm['farm']['seed'] = seed
                    cfg.farm['simulation']['market'].setdefault('realism', {}).update(SCENARIOS[scenario])
                    if name == 'control':
                        cfg.farm['evolution'].update(retire_fraction=0., immigration_rate=0.)
                    arms[name] = arm(cfg, generations, name, progress)
                if arms['treatment']['initial_population_digest'] != arms['control']['initial_population_digest']:
                    raise RuntimeError('seed populations do not match')
                if abs(arms['treatment']['baseline_net']-arms['control']['baseline_net']) > 1e-5:
                    raise RuntimeError('pre-treatment baseline is not balanced; pair invalid')
                # Paths stay valid when a retained campaign is moved between hosts.
                for result in arms.values():
                    result['ledger'] = str(Path(result['ledger']).relative_to(root))
                pair = {'scenario': scenario, 'seed': seed, **arms,
                        'delta_post_baseline_net': arms['treatment']['post_baseline_net']-arms['control']['post_baseline_net']}
                t, c = arms['treatment']['guardrails'], arms['control']['guardrails']
                pair['relative_call_imbalance'] = abs(t['steps']-c['steps'])/max(1, c['steps'])
                pair['guardrails_pass'] = (t['policy_violations'] == c['policy_violations'] == 0
                    and t['step_exceptions'] == c['step_exceptions'] == 0
                    and t['failed_inference'] <= c['failed_inference']
                    and t['malformed_rate'] <= c['malformed_rate']+.01
                    and pair['relative_call_imbalance'] <= .05)
                pairs.append(pair)
                folder = root/scenario/str(seed)
                (folder/'pair.json').write_text(json.dumps(pair, indent=2)+'\n', encoding='utf-8')
    except BaseException as exc:
        (root/'incomplete.json').write_text(json.dumps({'error': str(exc), 'completed_pairs': len(pairs),
            'status': 'incomplete; no validity claim'}, indent=2)+'\n', encoding='utf-8')
        raise
    report = {'plan': plan, 'plan_digest': digest(plan), 'pairs': pairs, **summarize(pairs)}
    (root/'report.json').write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
    lines = ['# Market validity campaign', '', 'Real-world validity: **unestablished**.', '',
             f"Simulation screen: **{report['simulation_screen']}**.", '',
             '| Scenario | Seed pairs | Mean profit after tail | Evolution delta | Losing runs |',
             '|---|---:|---:|---:|---:|']
    for scenario, r in report['scenarios'].items():
        lines.append(f"| {scenario} | {r['independent_seed_pairs']} | {r['mean_net_after_tail']:.2f} | {r['mean_delta']:.2f} | {r['loss_fraction']:.0%} |")
    lines += ['', report['interpretation'], '', 'Every paired ledger and audit is retained next to report.json.',
              '', 'Scripted inference does not test Qwen. Synthetic demand and assumed product quality cannot prove that customers will pay.']
    (root/'report.md').write_text('\n'.join(lines)+'\n', encoding='utf-8')
    (root/'incomplete.json').unlink()
    return report
