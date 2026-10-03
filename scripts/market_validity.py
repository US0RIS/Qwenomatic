"""Run a predeclared market stress campaign through the protected launcher."""
import argparse
from supervisor.config import FarmConfig
from supervisor.experiments.market_validity import run_campaign


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config-dir', required=True)
    p.add_argument('--root', required=True)
    p.add_argument('--pairs', type=int, default=3)
    p.add_argument('--generations', type=int, default=7)
    a = p.parse_args()
    if not 1 <= a.pairs <= 100:
        p.error('--pairs must be between 1 and 100')
    cfg = FarmConfig.load(a.config_dir)
    seeds = [cfg.seed + 1009*i for i in range(a.pairs)]
    report = run_campaign(a.config_dir, a.root, seeds=seeds, generations=a.generations)
    print('Simulation screen:', report['simulation_screen'])
    print('Real-world validity:', report['real_world_validity'])
    print('Report:', a.root + '/report.md')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
