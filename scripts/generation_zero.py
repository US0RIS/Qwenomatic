#!/usr/bin/env python3
"""Run Generation Zero (and the start of Generation One) and print the evidence.

    python scripts/generation_zero.py --data-dir var/gen0
    python scripts/generation_zero.py --backend openai_compatible --wall-clock   # real local model, real time

With the default simulated backend and clock this completes a 24-hour
generation in seconds. With `--wall-clock` it takes 24 real hours.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from storage.events import EventType  # noqa: E402
from supervisor.audit import verify  # noqa: E402
from supervisor.config import FarmConfig  # noqa: E402
from supervisor.core import Supervisor  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--data-dir", default="var/generation-zero")
    p.add_argument("--generations", type=int, default=1)
    p.add_argument("--backend", choices=["simulated", "openai_compatible"], default=None)
    p.add_argument("--wall-clock", action="store_true")
    args = p.parse_args()

    overrides: dict = {"farm": {}}
    if args.backend:
        overrides["farm"]["inference"] = {"backend": args.backend}
    if args.wall_clock:
        overrides["farm"]["clock"] = {"mode": "wall"}
    cfg = FarmConfig.load(data_dir=Path(args.data_dir).resolve(), overrides=overrides)
    sup = Supervisor(cfg)
    try:
        sup.bootstrap()
        start = sup.state.current_generation
        sup.run(generations=args.generations)
        for g in range(start, sup.state.current_generation):
            fit = sup.state.fitness[g]
            plan = sup.state.selections[g]["plan"]
            cs = sup.state.counters[g]
            print(f"\n== generation {g}")
            print(f"agents evaluated      {len(fit)}  (eligible {sum(r['eligible'] for r in fit.values())})")
            print(f"steps / GPU-seconds   {sum(c.steps for c in cs.values())} / {sum(c.gpu_seconds for c in cs.values()):.0f}")
            print(f"gross / net realized  {sum(c.gross_revenue for c in cs.values()):,.2f} / "
                  f"{sum(c.net_realized for c in cs.values()):,.2f}  (simulated economy)")
            print(f"elites                {', '.join(a[:8] for a in plan['elites'])}")
            print(f"retired               {len(plan['retirements'])}")
            print(f"offspring/immigrants  {sum(o['origin'] == 'offspring' for o in plan['offspring'])}/"
                  f"{sum(o['origin'] == 'immigrant' for o in plan['offspring'])}")
        print("\n== audit")
        report = verify(sup)
        print(json.dumps({k: v for k, v in report.items() if k != "accounting"}, indent=2))
        print(f"accounting replay ok: {report['accounting']['ok']}")
        print(f"human interventions:  {len(sup.store.iter_events(types=[EventType.HUMAN_INTERVENTION]))}")
        print(f"ledger:               {cfg.data_dir / 'ledger.sqlite3'}")
    finally:
        sup.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
