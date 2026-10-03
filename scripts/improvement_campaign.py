"""Run randomized, reversible feature experiments without enabling live features."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from supervisor.experiments.improvements import run_campaign
from supervisor.improvements import FEATURES


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--feature', choices=FEATURES, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--config-dir', type=Path)
    p.add_argument('--seeds', type=int, nargs='+', default=[101,202])
    p.add_argument('--generations', type=int, default=3)
    p.add_argument('--changes', type=json.loads)
    p.add_argument('--candidate', choices=['exploration','retirement','mutation','reserve'])
    p.add_argument('--quick', action='store_true')
    a = p.parse_args()
    overrides = {'farm': {'generation': {'duration_hours': 6, 'tick_seconds': 900}},
                 'fitness': {'min_exposure_steps': 5}} if a.quick else None
    if a.candidate:
        a.changes = {'exploration':{'scheduler.exploration_share':.4},
                     'retirement':{'evolution.retire_fraction':.25},
                     'mutation':{'mutation.price.sigma':.1},
                     'reserve':{'improvements.fraud.reserve_fraction':.3}}[a.candidate]
    report = run_campaign(a.output, a.feature, seeds=a.seeds, generations=a.generations,
                          config_dir=a.config_dir, changes=a.changes, overrides=overrides)
    path = a.output/'campaign-report.json'
    path.write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
    print(f'{report["status"]}: {path}; mean delta net={report["mean_delta_net"]:.3f}; not promoted')
    return 0

if __name__ == '__main__':
    main()
