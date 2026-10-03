"""Fit local, deidentified observations; preserve originals and all holdout results."""
import argparse
import json
from pathlib import Path

import yaml
from supervisor.config import FarmConfig, deep_merge
from supervisor.experiments.market_calibration import calibrate


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--observations', required=True)
    p.add_argument('--holdout-after', required=True, help='preselected timestamp with timezone')
    p.add_argument('--source-description', required=True)
    p.add_argument('--config-dir', required=True)
    p.add_argument('--output-config', required=True, help='new directory; never overwrites an existing profile')
    a = p.parse_args()
    cfg = FarmConfig.load(a.config_dir)
    report = calibrate(a.observations, cfg.farm['simulation']['market'], a.holdout_after, a.source_description)
    if not report['parameter_patch']:
        raise SystemExit('Insufficient training/holdout data; no configuration created.')
    output = Path(a.output_config)
    output.mkdir(parents=True, exist_ok=False)
    cfg.farm['simulation']['market'] = deep_merge(cfg.farm['simulation']['market'],
        {'model': 'constrained_v1', 'segments': report['parameter_patch']})
    for name, value in [('farm', cfg.farm), ('policy', cfg.policy), ('fitness', cfg.fitness)]:
        (output/f'{name}.yaml').write_text(yaml.safe_dump(value, sort_keys=False), encoding='utf-8')
    (output/'calibration-report.json').write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
    print(f'Observational profile written to {output}; real-world validity remains unestablished.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
