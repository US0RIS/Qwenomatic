"""Bounded settings proposals, isolated campaigns, explicit operator decisions."""
from __future__ import annotations
import copy
from storage.events import EventType, digest
from .improvements import emit

BOUNDS = {'scheduler.exploration_share': (.1, .6), 'evolution.retire_fraction': (.1, .5),
          'mutation.price.sigma': (.05, .3), 'improvements.fraud.reserve_fraction': (.1, .5)}


def validate_changes(changes):
    if not isinstance(changes, dict) or not changes or set(changes) - set(BOUNDS):
        raise ValueError('only bounded exploration, retirement, mutation and reserve settings may be proposed')
    for key, v in changes.items():
        low, high = BOUNDS[key]
        if type(v) not in (int, float) or not low <= v <= high:
            raise ValueError(f'{key} outside approved proposal bounds')


def apply_changes(farm, changes):
    validate_changes(changes)
    for path, value in changes.items():
        target = farm
        parts = path.split('.')
        for part in parts[:-1]:
            target = target[part]
        target[parts[-1]] = value
    if 'scheduler.exploration_share' in changes:
        farm['scheduler']['exploitation_share'] = 1 - farm['scheduler']['exploration_share']


def propose_tuning(sup, changes, campaign):
    if not sup.improvements.cfg['self_tuning']['enabled']:
        raise ValueError('self tuning is disabled')
    validate_changes(changes)
    if (campaign.get('feature') != 'self_tuning' or campaign.get('changes') != changes
            or campaign.get('status') != 'simulation_verified' or len(set(campaign.get('seeds', []))) < 2
            or campaign.get('baseline_hash') != digest(sup.config.farm)):
        raise ValueError('a matching multi-seed campaign against the current config is required')
    proposal_id = digest([changes, campaign, sup.config.hashes()])
    e = emit(sup, EventType.TUNING_PROPOSED, {'proposal_id': proposal_id, 'changes': changes,
             'campaign': campaign, 'base_hash': digest(sup.config.farm), 'status': 'awaiting_operator'},
             'tuning-proposal:' + proposal_id, generation=sup.state.current_generation)
    return e.payload['proposal_id']


def resolve_tuning(sup, proposal_id, granted, operator, note):
    e = sup.store.get_by_idempotency('tuning-proposal:' + proposal_id)
    if not e or type(granted) is not bool or not operator or not note.strip():
        raise ValueError('known proposal, decision, operator and note required')
    if e.payload['base_hash'] != digest(sup.config.farm):
        raise ValueError('stale config: re-run campaign')
    validate_changes(e.payload['changes'])
    emit(sup, EventType.TUNING_RESOLVED, {'proposal_id': proposal_id, 'granted': granted,
         'operator': operator, 'note': note, 'base_hash': e.payload['base_hash'],
         'effective_generation': (sup.state.current_generation or 0) + 1},
         'tuning-resolution:' + proposal_id, author='operator:' + operator,
         generation=sup.state.current_generation)


def effective_settings(sup, cfg):
    # Changes enter only a new generation's frozen config. Files are never overwritten.
    if not sup.improvements.cfg['self_tuning']['enabled']:
        return
    for r in sup.store.iter_events(types=[EventType.TUNING_RESOLVED]):
        if r.payload['granted'] and r.payload['base_hash'] == digest(sup.config.farm):
            p = sup.store.get_by_idempotency('tuning-proposal:' + r.payload['proposal_id'])
            changes = p.payload['changes']
            for path, value in changes.items():
                apply_changes(cfg, {path: value})
    # Mutation is separately frozen per generation and applied by the generation manager.


def run_tuning(sup):
    """Supervisor-owned offline R&D; one fixed candidate per run, with holdout seeds."""
    import tempfile
    from pathlib import Path
    from supervisor.experiments.improvements import run_campaign
    if sup.boundary.manifest.get('model_url') is not None or sup.boundary.manifest['adapters']:
        raise ValueError('run settings campaigns in a separate protected simulation launch; submit the report for review')
    g = sup.state.current_generation or 0
    keys = list(BOUNDS)
    key = keys[g % len(keys)]
    candidates = {'scheduler.exploration_share': .4, 'evolution.retire_fraction': .25,
                  'mutation.price.sigma': .1, 'improvements.fraud.reserve_fraction': .3}
    changes = {key: candidates[key]}
    # Source context is copied to the simulation; routes and live backend are never used.
    with tempfile.TemporaryDirectory(prefix='qwen-tuning-') as tmp:
        report = run_campaign(Path(tmp), 'self_tuning', seeds=(sup.config.seed+17, sup.config.seed+31),
                              generations=2, config_dir=sup.config.config_dir, changes=changes,
                              overrides={'farm': sup.config.farm, 'policy': sup.config.policy,
                                         'fitness': sup.config.fitness})
    return propose_tuning(sup, changes, report)
