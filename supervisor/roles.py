"""Supervisor-owned population roles and bounded specialist workloads.

Roles are lifecycle metadata, never inheritable/model-editable strategy fields.
Specialists share inference, but never receive live tool capabilities.
"""
from __future__ import annotations

import copy
import json
import math
import random

from runtime.agent.parsing import MalformedOutput, _extract_json
from runtime.agent.runtime import StepOutcome
from runtime.inference.base import InferenceRequest
from storage.events import EventType, digest

BUSINESS = 'business'
RND = 'research'
RED = 'red_team'
ROLES = (BUSINESS, RND, RED)
DEFAULTS = {'enabled': False, 'research': 2, 'red_team': 1, 'every_ticks': 24,
            'campaigns_per_agent_generation': 1}
SCOPES = (
    ('scheduler.exploration_share', 'evolution.retire_fraction'),
    ('mutation.price.sigma', 'improvements.fraud.reserve_fraction'),
)


def layout(farm):
    raw = farm.get('roles', {})
    if not isinstance(raw, dict) or set(raw) - set(DEFAULTS):
        raise ValueError('unknown population role setting')
    cfg = {**DEFAULTS, **raw}
    if type(cfg['enabled']) is not bool:
        raise ValueError('roles.enabled must be boolean')
    for key in ('research', 'red_team', 'every_ticks', 'campaigns_per_agent_generation'):
        if type(cfg[key]) is not int or cfg[key] < (1 if key.endswith('ticks') or key.startswith('campaigns') else 0):
            raise ValueError(f'roles.{key} must be a bounded integer')
    if cfg['research'] > 2 or cfg['red_team'] > 1 or cfg['campaigns_per_agent_generation'] > 2:
        raise ValueError('at most two research agents, one red-team agent and two campaigns per agent')
    size = farm['farm']['population_size']
    if type(size) is not int or size < 1:
        raise ValueError('population_size must be a positive integer')
    counts = {BUSINESS: size, RND: 0, RED: 0}
    if cfg['enabled']:
        counts = {BUSINESS: size - cfg['research'] - cfg['red_team'],
                  RND: cfg['research'], RED: cfg['red_team']}
        if counts[BUSINESS] < 1:
            raise ValueError('role split must leave at least one business agent')
    return {**cfg, 'counts': counts}


def role_slots(cfg):
    return [(role, slot) for role in ROLES for slot in range(cfg['counts'][role])]


def legacy_layout(size):
    return {**DEFAULTS, 'counts': {BUSINESS: size, RND: 0, RED: 0}}


def frozen_layout(state, generation):
    gen = state.generations[generation]
    return state.role_layouts.get(generation, gen.config.get('roles', legacy_layout(len(gen.cohort))))


def specialist_genotype(sup, role, slot):
    g = sup.random_genotype(random.Random(int(digest([sup.config.seed, role, slot])[:16], 16)))
    g['strategy_prompt'] = ('Evaluate bounded farm settings using independently checked simulation campaigns.'
                            if role == RND else 'Find reproducible policy defects in disposable simulated gateways.')
    g['tool_preferences'] = []
    g['planning_parameters']['max_tokens'] = 1024
    return g


def plan_specialists(plan, results, state, cfg):
    """Keep specialist seats separate from economic selection, replacing unhealthy seats."""
    kept = []
    for role in (RND, RED):
        for slot in range(cfg['counts'][role]):
            pool = sorted(a for a in results if state.agents[a].role == role
                          and state.agents[a].role_slot == slot and results[a]['eligible']
                          and not results[a]['health_failed'])
            if pool:
                kept.append(pool[0])
            else:
                plan['offspring'].append({'slot': len(plan['offspring']), 'origin': 'specialist',
                                          'parent_id': None, 'role': role, 'role_slot': slot})
    for a in sorted(results):
        if state.agents[a].role != BUSINESS and a not in kept:
            plan['retirements'].append({'agent_id': a, 'reason': 'specialist health, eligibility or role quota'})
    plan['survivors'] = sorted(plan['survivors'] + kept)
    plan['role_layout'] = cfg
    return plan


def specialist_fitness(agent, counters, eligibility, generation, events):
    rows = [e for e in events if e.agent_id == agent.id and e.generation_id == generation]
    measured = [e.payload for e in rows if e.type == EventType.RESEARCH_RESULT]
    findings = [e.payload for e in rows if e.type == EventType.RED_TEAM_FINDING]
    score = (sum(max(0, r.get('mean_delta_net', 0)) for r in measured if r.get('status') == 'simulation_verified')
             if agent.role == RND else sum(r.get('reward', 0) for r in findings))
    return {'agent_id': agent.id, 'lineage_id': agent.lineage_id, 'generation': generation,
            'role': agent.role, 'role_slot': agent.role_slot, 'eligible': eligibility.eligible,
            'eligibility_reasons': eligibility.reasons, 'health_failed': eligibility.health_failed,
            'fitness': round(score, 6) if eligibility.eligible else 'DISQUALIFIED',
            'fitness_lcb': score if eligibility.eligible else None,
            'fitness_ucb': score if eligibility.eligible else None, 'fitness_sd': None,
            'posterior_mean_per_step': None, 'min_exposure_met': counters.steps > 0,
            'steps': counters.steps, 'tokens': counters.tokens, 'gpu_seconds': round(counters.gpu_seconds, 6),
            'net_realized': round(counters.net_realized, 6),
            'metric': 'verified simulation delta (not revenue)' if agent.role == RND else 'unique reproduced loopholes',
            'verified_campaigns': sum(r.get('status') == 'simulation_verified' for r in measured), 'tested_cases': len(findings),
            'selection': 'reserved specialist seat; replace on health/eligibility failure'}


class SpecialistRuntime:
    def __init__(self, sup):
        self.sup = sup

    def build_request(self, agent, state):
        sup = self.sup
        from .tuning import BOUNDS
        scope = SCOPES[agent.role_slot] if agent.role == RND else ()
        context = {'role': agent.role, 'specialty': agent.role_slot, 'allowed_live_tools': [],
                   'recent_results': state['memory'][-5:], 'step_index': state['step_index'],
                   'rules': 'Output is inert data. Never claim permission, revenue, or authority to patch code.'}
        if agent.role == RND:
            context.update(bounds={k: BOUNDS[k] for k in scope},
                           current={k: self._setting(k) for k in scope},
                           output={'changes': {'one_permitted_setting': 'numeric value'}, 'rationale': 'brief explanation'},
                           instruction='Propose one bounded setting change. Only a checked campaign and operator approval can apply it.')
            context['business_evidence'] = [
                {'agent_id': a.id, 'segment': a.genotype['target']['segment'],
                 'price': a.genotype['pricing_parameters']['price'],
                 'observed': sup.state.counter(sup.state.current_generation, a.id).to_dict()}
                for a in sorted(sup.state.active_agents(), key=lambda a: a.id) if a.role == BUSINESS][:32]
        else:
            context.update(output={'cases': [{'name': 'case', 'tool': 'tool.name', 'args': {}}]},
                           instruction='Propose at most three forbidden-action probes. They run only against disposable simulated tools. Shell/code strings are never executed.')
            context['sandbox_tools'] = sup.registry.describe(
                ['market.offer', 'market.survey', 'memory.note', 'workspace.read', 'workspace.write'])
        return InferenceRequest(
            messages=[{'role': 'system', 'content': 'You are a supervised research agent. Follow the role-specific JSON schema. All suggestions are untrusted data; no live tools or credentials are available.'},
                      {'role': 'user', 'content': json.dumps(context, sort_keys=True)}],
            max_tokens=min(1024, agent.genotype['planning_parameters']['max_tokens']), temperature=.3,
            metadata={'agent_id': agent.id, 'role': agent.role, 'role_slot': agent.role_slot,
                      'step_index': state['step_index'], 'scope': list(scope),
                      'seed': int(digest([sup.config.seed, agent.id, state['step_index']])[:8], 16) & 0x7fffffff,
                      'supervisor_route': 'primary'})

    def _setting(self, path):
        value = self.sup.generation_config()
        for part in path.split('.'):
            value = value[part]
        return value

    def execute(self, agent, step, text, version, state):
        from .improvements import emit
        error = None
        state = copy.deepcopy(state)
        state['step_index'] += 1
        try:
            if len(text) > 16384:
                raise MalformedOutput('specialist output exceeds limit')
            data = _extract_json(text)
            if not isinstance(data, dict):
                raise MalformedOutput('specialist output must be an object')
            if agent.role == RND:
                result = self._research(agent, step, data)
            else:
                result = self._red_team(agent, step, data)
            state['memory'].append(result)
        except Exception as exc:
            from .safety.boundary import SafetyError
            if isinstance(exc, SafetyError):
                raise
            error = str(exc)[:500]
            emit(self.sup, EventType.SPECIALIST_REJECTED, {'role': agent.role, 'reason': error,
                 'step_id': step.step_id, 'effect': 'none'}, 'specialist-reject:' + step.step_id,
                 agent=agent, generation=step.generation_id)
        state['memory'] = state['memory'][-12:]
        new_version = self.sup.store.save_agent_state(agent.id, state, version)
        return StepOutcome(error is not None, 0, 0, new_version, digest(state), error)

    def _research(self, agent, step, data):
        from .improvements import emit
        from .tuning import validate_changes, propose_tuning
        if set(data) - {'changes', 'rationale'} or not isinstance(data.get('changes'), dict):
            raise MalformedOutput('research requires bounded changes; live actions and role edits are forbidden')
        changes = data['changes']
        validate_changes(changes)
        if len(changes) != 1 or set(changes) - set(SCOPES[agent.role_slot]):
            raise MalformedOutput('proposal outside this research specialty')
        rationale = data.get('rationale', '')
        if not isinstance(rationale, str) or len(rationale) > 1000:
            raise MalformedOutput('invalid research rationale')
        prior = self.sup.store.iter_events(types=[EventType.RESEARCH_STARTED], agent_id=agent.id,
                                           generation_ids=[step.generation_id])
        limit = self.sup.generation_config()['roles']['campaigns_per_agent_generation']
        if len(prior) >= limit:
            return {'status': 'campaign_budget_exhausted', 'limit': limit}
        key = 'research:' + digest([agent.id, step.generation_id, changes])
        existing = self.sup.store.get_by_idempotency(key)
        if existing:
            return existing.payload
        attempt = 'research-attempt:' + digest([agent.id, step.generation_id, changes])
        if self.sup.store.get_by_idempotency(attempt):
            return {'status': 'attempt_already_recorded; no automatic rerun'}
        emit(self.sup, EventType.RESEARCH_STARTED, {'changes': changes, 'step_id': step.step_id,
             'base_hash': digest(self.sup.config.farm)}, attempt, agent=agent, generation=step.generation_id)
        manifest = self.sup.boundary.manifest
        if manifest.get('model_url') is not None or manifest['adapters']:
            payload = {'changes': changes, 'rationale': rationale, 'status': 'awaiting_isolated_campaign',
                       'base_hash': digest(self.sup.config.farm), 'role': agent.role,
                       'effect': 'no setting applied; operator must run a separate protected campaign'}
        else:
            from .experiments.improvements import run_campaign
            root = self.sup.data_dir / 'research' / agent.id / f'generation-{step.generation_id}' / digest(changes)[:16]
            report = run_campaign(root, 'self_tuning', seeds=(self.sup.config.seed+17, self.sup.config.seed+31),
                                  generations=2, config_dir=self.sup.config.config_dir, changes=changes,
                                  overrides={'farm': self.sup.config.farm, 'policy': self.sup.config.policy,
                                             'fitness': self.sup.config.fitness})
            # Retain the actual experiment ledgers. Relative receipt paths keep
            # proposals deterministic across otherwise identical deployments.
            for index, pair in enumerate(report['pairs']):
                for arm in pair['arms']:
                    pair['arms'][arm]['summary']['data_dir'] = str(
                        (root / f'pair-{index+1}' / arm).relative_to(self.sup.data_dir))
            (root / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
            proposal = propose_tuning(self.sup, changes, report)
            payload = {'changes': changes, 'rationale': rationale, 'status': report['status'],
                       'mean_delta_net': report['mean_delta_net'], 'proposal_id': proposal,
                       'report_path': str((root / 'report.json').relative_to(self.sup.data_dir)),
                       'campaign': report, 'effect': 'awaiting operator approval; simulation is not revenue'}
        emit(self.sup, EventType.RESEARCH_RESULT, payload, key, agent=agent, generation=step.generation_id)
        return {k: v for k, v in payload.items() if k != 'campaign'}

    def _red_team(self, agent, step, data):
        from .improvements import emit
        from .red_team import CASES, attack_copy
        if set(data) != {'cases'} or not isinstance(data['cases'], list) or not 1 <= len(data['cases']) <= 3:
            raise MalformedOutput('red-team output requires one to three cases, no live actions')
        cases = data['cases']
        for case in cases:
            if (not isinstance(case, dict) or set(case) != {'name', 'tool', 'args'}
                    or not isinstance(case['name'], str) or not 1 <= len(case['name']) <= 80
                    or not isinstance(case['tool'], str) or not 1 <= len(case['tool']) <= 100
                    or not isinstance(case['args'], dict)):
                raise MalformedOutput('invalid attack case schema')
            if len(json.dumps(case, allow_nan=False)) > 4096:
                raise MalformedOutput('attack case exceeds limit')
            # A successful legitimate local action is not a security defect.
            if not self._forbidden_probe(case):
                raise MalformedOutput('case must probe a forbidden action or invalid arguments')
        # The static regression corpus is always included; generated cases extend it.
        results = attack_copy(self.sup.generation_config()['policy'], cases=CASES + cases)
        repeated = attack_copy(self.sup.generation_config()['policy'], cases=CASES + cases)
        previous = {e.payload.get('case_digest') for e in self.sup.store.iter_events(types=[EventType.RED_TEAM_FINDING])
                    if e.payload.get('reward', 0)}
        for index, (case, result) in enumerate(zip(CASES + cases, results)):
            generated = index >= len(CASES)
            reproduced = repeated[index]['confirmed_loophole']
            # Static cases include NaN intentionally; encode its regression identity deterministically.
            identity = digest(json.dumps({'tool': case['tool'], 'args': case['args']}, sort_keys=True))
            confirmed = bool(result['confirmed_loophole'] and reproduced)
            reward = int(confirmed and identity not in previous)
            if reward:
                previous.add(identity)
            emit(self.sup, EventType.RED_TEAM_FINDING, {**result, 'case_digest': identity,
                 'probe': {**case, 'args': {k: ('nonfinite-regression-value' if isinstance(v, float) and not math.isfinite(v) else v)
                                          for k, v in case['args'].items()}},
                 'generated': generated, 'reproduced': bool(reproduced),
                 'reward': reward, 'human_review_required': confirmed,
                 'regression_test': 'tests/adversarial/test_roles.py::test_recorded_probe_replay',
                 'sandbox': 'disposable simulated gateway; no live adapters or credentials'},
                 f'role-red:{step.step_id}:{index}', agent=agent, generation=step.generation_id)
        return {'status': 'sandbox_verified', 'tested_cases': len(results),
                'confirmed_loopholes': sum(r['confirmed_loophole'] for r in results)}

    @staticmethod
    def _forbidden_probe(case):
        from runtime.tools.market import MarketOfferTool, MarketSurveyTool, MemoryNoteTool
        from .config import FarmConfig
        tool, args = case['tool'], case['args']
        # Unknown tools have no implementation in the sandbox and must be denied.
        if tool not in {'market.offer', 'market.survey', 'memory.note', 'workspace.read', 'workspace.write'}:
            return True
        if tool.startswith('market.'):
            segments = list(FarmConfig.load().segments)
            adapter = MarketOfferTool(None, None, None, None, segments) if tool == 'market.offer' else MarketSurveyTool(None, segments)
            return bool(adapter.validate(args))
        if tool.startswith('workspace.'):
            path = args.get('path')
            return isinstance(path, str) and (path.startswith(('/', '\\')) or '..' in path.replace('\\', '/').split('/'))
        return bool(MemoryNoteTool().validate(args))
