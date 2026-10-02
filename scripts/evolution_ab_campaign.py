#!/usr/bin/env python3
"""Run multiple counterbalanced evolved-vs-frozen-control paired trials.

This is the highest-information unattended experiment for Qwenomatic's current
simulated economy. Each pair uses a distinct farm seed. Arm order alternates
between pairs to reduce order/thermal/cache bias.

Example:

    python scripts/evolution_ab_campaign.py --pairs 2 --generations 2 \
      --backend openai_compatible \
      --base-url http://127.0.0.1:11434/v1 \
      --model qwen3:14b \
      --max-concurrency 2

The campaign is resumable. Re-run the same command after Ctrl+C; completed
ledgers are reused. Use --reset only when intentionally starting over.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from supervisor.config import FarmConfig  # noqa: E402
from supervisor.experiments.evolution_ab import (  # noqa: E402
    compare,
    run_arm,
    write_campaign_report,
    write_report,
)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--config-dir", default=None)
    p.add_argument("--root", default="var/evolution-ab-campaign")
    p.add_argument("--pairs", type=int, default=2)
    p.add_argument("--generations", type=int, default=2,
                   help="closed generations per arm; Generation 0 is baseline")
    p.add_argument("--backend", choices=["simulated", "openai_compatible"], default=None)
    p.add_argument("--base-url", default=None)
    p.add_argument("--model", default=None)
    p.add_argument("--max-concurrency", type=int, default=None)
    p.add_argument("--seed", type=int, default=None,
                   help="base seed; each pair derives a deterministic distinct seed")
    p.add_argument("--seed-stride", type=int, default=1009)
    p.add_argument("--reset", action="store_true")
    args = p.parse_args()

    if args.pairs < 1:
        p.error("--pairs must be >= 1")
    if args.generations < 2:
        p.error("--generations must be >= 2")

    cfg = FarmConfig.load(args.config_dir)
    args.seed = cfg.seed if args.seed is None else args.seed
    args.backend = args.backend or cfg.farm["inference"]["backend"]
    args.max_concurrency = args.max_concurrency or cfg.farm["inference"].get("openai_compatible", {}).get("max_concurrency", 2)
    root = Path(args.root).resolve()
    if args.reset and root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)

    reports = []
    for i in range(args.pairs):
        pair_no = i + 1
        pair_seed = args.seed + i * args.seed_stride
        pair_root = root / f"pair-{pair_no:02d}"
        pair_root.mkdir(parents=True, exist_ok=True)
        order = ["treatment", "control"] if i % 2 == 0 else ["control", "treatment"]

        print(f"\n{'=' * 72}")
        print(f"PAIR {pair_no}/{args.pairs} | seed {pair_seed} | order {' -> '.join(order)}")
        print(f"{'=' * 72}")

        summaries = {}
        common = dict(
            config_dir=args.config_dir,
            generations=args.generations,
            backend=args.backend,
            base_url=args.base_url,
            model=args.model,
            max_concurrency=args.max_concurrency,
            seed=pair_seed,
        )
        for arm in order:
            summaries[arm] = run_arm(
                arm=arm,
                data_dir=pair_root / arm,
                **common,
            )

        report = compare(summaries["treatment"], summaries["control"])
        write_report(pair_root, report)
        (pair_root / "meta.json").write_text(
            json.dumps({"pair": pair_no, "seed": pair_seed, "order": order}, indent=2),
            encoding="utf-8",
        )
        reports.append(report)

        e = report["primary_effect"]
        print(
            f"PAIR {pair_no} RESULT | raw delta {e['delta_net_realized']:+.2f} | "
            f"baseline-adjusted delta/call "
            f"{e['difference_in_differences_net_per_call']:+.6f}"
        )

    jp, mp = write_campaign_report(root, reports)
    payload = json.loads(jp.read_text(encoding="utf-8"))
    s = payload["summary"]

    print(f"\n{'=' * 72}")
    print("CAMPAIGN RESULT")
    print(f"{'=' * 72}")
    print(f"pairs completed                {s['pairs']}")
    print(f"positive adjusted pairs        {s['positive_baseline_adjusted_pairs']}/{s['pairs']}")
    print(f"mean raw delta                 {s['mean_raw_delta_net']:+.2f}")
    print(f"mean adjusted delta/call       {s['mean_difference_in_differences_net_per_call']:+.6f}")
    print(f"equal call counts              {s['all_call_counts_equal']}")
    print(f"report                         {mp}")
    print(f"raw JSON                       {jp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
