#!/usr/bin/env python3
"""Profile inference behavior from a Qwenomatic ledger."""

from __future__ import annotations

import argparse
import math
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from storage.events import EventStore, EventType  # noqa: E402


def pct(values, p):
    if not values:
        return 0.0
    xs = sorted(values)
    i = max(0, min(len(xs) - 1, math.ceil(p * len(xs)) - 1))
    return xs[i]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="var/real-smart")
    args = ap.parse_args()

    store = EventStore(Path(args.data_dir) / "ledger.sqlite3", read_only=True)
    try:
        submitted = {
            e.payload["job_id"]: e.payload
            for e in store.iter_events(types=[EventType.INFERENCE_JOB_SUBMITTED])
        }
        completed = store.iter_events(types=[EventType.INFERENCE_JOB_COMPLETED])
    finally:
        store.close()

    if not completed:
        print("no completed inference jobs")
        return 1

    prompt = []
    completion = []
    wall = []
    queue = []
    gpu = []
    toks_per_sec = []
    max_hits = 0
    for e in completed:
        u = e.payload["usage"]
        pt = int(u["prompt_tokens"])
        ct = int(u["completion_tokens"])
        ws = float(u["wall_seconds"])
        q = float(u.get("queue_latency_seconds", 0.0))
        gs = float(u.get("gpu_seconds", 0.0))
        prompt.append(pt)
        completion.append(ct)
        wall.append(ws)
        queue.append(q)
        gpu.append(gs)
        if ws > 0:
            toks_per_sec.append(ct / ws)
        sub = submitted.get(e.payload["job_id"])
        if sub and int(sub.get("max_tokens", 0)) > 0 and ct >= 0.95 * int(sub["max_tokens"]):
            max_hits += 1

    n = len(completed)
    print(f"calls                    {n}")
    print(f"prompt tokens total      {sum(prompt):,}")
    print(f"completion tokens total  {sum(completion):,}")
    print(f"avg prompt tokens/call   {statistics.mean(prompt):.1f}")
    print(f"avg completion/call      {statistics.mean(completion):.1f}")
    print(f"p50 completion/call      {pct(completion, .50):.0f}")
    print(f"p95 completion/call      {pct(completion, .95):.0f}")
    print(f"p50 wall sec/call        {pct(wall, .50):.2f}")
    print(f"p95 wall sec/call        {pct(wall, .95):.2f}")
    print(f"p50 queue sec/call       {pct(queue, .50):.3f}")
    print(f"p95 queue sec/call       {pct(queue, .95):.3f}")
    print(f"median output tok/s      {statistics.median(toks_per_sec):.2f}")
    print(f"recorded GPU seconds     {sum(gpu):,.1f}")
    print(f"calls at >=95% max       {max_hits}/{n} ({100*max_hits/n:.2f}%)")
    print()
    print("Interpretation:")
    if statistics.median(queue) > 1.0:
        print("- Material client-side queueing is present; match Qwenomatic concurrency to the server.")
    else:
        print("- Client-side queueing is small.")
    if max_hits / n > 0.05:
        print("- Many responses hit the token ceiling; reducing max_tokens would risk truncation.")
    else:
        print("- Few responses hit the token ceiling; max_tokens is mostly a safety ceiling, not the main runtime driver.")
    if sum(completion) > sum(prompt):
        print("- Generated tokens exceed prompt tokens; decode is likely the dominant inference cost.")
    else:
        print("- Prompt volume is substantial; prompt/cache optimization may matter.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
