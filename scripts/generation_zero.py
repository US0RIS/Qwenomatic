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
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from storage.events import EventType  # noqa: E402
from supervisor.audit import verify  # noqa: E402
from supervisor.config import FarmConfig  # noqa: E402
from supervisor.core import Supervisor  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--config-dir", default=None)
    p.add_argument("--data-dir", default="var/generation-zero")
    p.add_argument("--generations", type=int, default=1)
    p.add_argument("--ticks", type=int, help="stop after this many ticks to verify a short real-model run")
    p.add_argument("--backend", choices=["simulated", "openai_compatible"], default=None)
    p.add_argument("--wall-clock", action="store_true")
    args = p.parse_args()
    if args.ticks is not None and args.ticks < 1:
        p.error("--ticks must be positive")

    overrides: dict = {"farm": {}}
    if args.backend:
        overrides["farm"]["inference"] = {"backend": args.backend}
    if args.wall_clock:
        overrides["farm"]["clock"] = {"mode": "wall"}
    cfg = FarmConfig.load(args.config_dir, data_dir=Path(args.data_dir).resolve(), overrides=overrides)
    sup = Supervisor(cfg)
    try:
        sup.bootstrap()
        print(f"Market: {sup.market.model}; configured decision backend: {sup.backend.name}", flush=True)
        print('Synthetic customers and assumed delivery quality; real-world validity is unestablished.')
        if sup.backend.name == 'simulated':
            print('Scripted policy emulator: Qwen is not running; GPU time is emulated.')
        start = sup.state.current_generation
        target = start + args.generations
        started = time.monotonic()
        ticks = 0
        proved = bool(sup.store.iter_events(types=[EventType.INFERENCE_JOB_COMPLETED],
                                            generation_ids=[start], limit=1))
        while sup.state.current_generation < target and (args.ticks is None or ticks < args.ticks):
            result = sup.tick()
            if result.halted:
                raise RuntimeError(f"farm halted: {sup.state.halt_reason}")
            ticks += 1
            if sup.backend.name != 'simulated' and result.completed and not proved:
                proved = True
                print("Real-model inference verified: first completion recorded in the ledger.", flush=True)
            if ticks % 12 == 0 or result.closed_generation is not None or ticks == args.ticks:
                gv = sup.state.generations[sup.state.current_generation]
                print(f"Progress: generation {sup.state.current_generation}, "
                      f"tick {sup.clock.tick - gv.start_tick}/{cfg.ticks_per_generation}, "
                      f"{(time.monotonic() - started)/60:.1f} wall minutes", flush=True)
            sup.clock.sleep_until_next_tick()
        if sup.backend.name != 'simulated' and not proved:
            raise RuntimeError("no successful real-model inference; run is invalid")
        if args.ticks is not None and sup.state.current_generation < target:
            print(f"Stopped after {ticks} tick(s); generation {sup.state.current_generation} remains open.", flush=True)
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
        print(f"pending settlements:  {len(sup.state.pending_settlements)} (use market-validity for complete payment/refund tails)")
        print(f"ledger:               {cfg.data_dir / 'ledger.sqlite3'}")
    finally:
        sup.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
