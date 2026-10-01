#!/usr/bin/env python3
"""Run a matched evolved-vs-frozen-control Qwenomatic experiment.

Example for the local Ollama server (OLLAMA_NUM_PARALLEL must match
--max-concurrency):

    python scripts/evolution_ab.py --generations 2 \
      --backend openai_compatible \
      --base-url http://127.0.0.1:11434/v1 \
      --model qwen3:14b \
      --max-concurrency 2

The command is resumable: rerun it after Ctrl+C and each arm continues from
its own persisted ledger.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from supervisor.config import FarmConfig  # noqa: E402
from supervisor.experiments.evolution_ab import compare, run_arm, write_report  # noqa: E402


def main() -> int:
    default_seed = FarmConfig.load().seed
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--root", default="var/evolution-ab")
    p.add_argument("--generations", type=int, default=2,
                   help="number of closed generations per arm; Generation 0 is baseline")
    p.add_argument("--backend", choices=["simulated", "openai_compatible"], default="simulated")
    p.add_argument("--base-url", default=None)
    p.add_argument("--model", default=None)
    p.add_argument("--max-concurrency", type=int, default=2,
                   help="must match actual inference-server parallelism for GPU accounting")
    p.add_argument("--seed", type=int, default=default_seed)
    p.add_argument("--order", choices=["treatment-first", "control-first"], default="treatment-first")
    p.add_argument("--reset", action="store_true", help="delete this experiment root before starting")
    args = p.parse_args()

    if args.generations < 2:
        p.error("--generations must be at least 2 (Generation 0 is the pre-treatment baseline)")

    root = Path(args.root).resolve()
    if args.reset and root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)

    common = dict(
        generations=args.generations,
        backend=args.backend,
        base_url=args.base_url,
        model=args.model,
        max_concurrency=args.max_concurrency,
        seed=args.seed,
    )
    summaries = {}
    order = ["treatment", "control"] if args.order == "treatment-first" else ["control", "treatment"]
    for arm in order:
        summaries[arm] = run_arm(arm=arm, data_dir=root / arm, **common)

    report = compare(summaries["treatment"], summaries["control"])
    jp, mp = write_report(root, report)

    e = report["primary_effect"]
    print("\n== A/B RESULT ==")
    print(f"post-baseline treatment net  {report['treatment']['post_baseline']['net_realized']:.2f}")
    print(f"post-baseline control net    {report['control']['post_baseline']['net_realized']:.2f}")
    print(f"evolution delta              {e['delta_net_realized']:+.2f}")
    print(f"equal call counts            {e['all_generation_call_counts_equal']}")
    print(f"report                       {mp}")
    print(f"raw JSON                     {jp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
