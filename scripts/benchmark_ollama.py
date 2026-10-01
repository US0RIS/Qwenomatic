#!/usr/bin/env python3
"""Benchmark aggregate Ollama/OpenAI-compatible inference throughput.

The server's OLLAMA_NUM_PARALLEL sets the maximum true server parallelism.
This script varies client concurrency and reports aggregate completion-token
throughput so you can choose the fastest setting empirically.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import statistics
import time
import urllib.request


SYSTEM = """You are an autonomous business agent in a supervised experiment.
Goal: legitimate externally verifiable net profit. Think carefully but
proportionally to the decision. Reply only with JSON:
{"thought":"short reasoning","actions":[{"tool":"market.offer","args":{"segment":"smb-bookkeeping","price":60}}],"memory":"short note"}"""


def call(base_url: str, model: str, seed: int, max_tokens: int):
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": "Step 12. Recent results: 3 offers, 1 conversion, acquisition cost $1. Decide next action."},
        ],
        "temperature": 0.4,
        "max_tokens": max_tokens,
        "seed": seed,
        "response_format": {"type": "json_object"},
    }
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=300) as resp:
        data = json.loads(resp.read())
    elapsed = time.perf_counter() - t0
    usage = data.get("usage", {})
    content = data["choices"][0]["message"].get("content") or ""
    try:
        json.loads(content)
        valid = True
    except Exception:
        valid = False
    return {
        "elapsed": elapsed,
        "prompt": int(usage.get("prompt_tokens", 0)),
        "completion": int(usage.get("completion_tokens", 0)),
        "valid": valid,
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--base-url", default="http://127.0.0.1:11434/v1")
    p.add_argument("--model", default="qwen3:14b")
    p.add_argument("--concurrency", default="1,2,3,4")
    p.add_argument("--requests", type=int, default=8)
    p.add_argument("--max-tokens", type=int, default=1024)
    p.add_argument("--json", action="store_true", help="emit machine-readable JSON after the benchmark")
    args = p.parse_args()

    levels = [int(x) for x in args.concurrency.split(",") if x.strip()]
    if not args.json:
        print("warming model...")
    call(args.base_url, args.model, 999001, min(args.max_tokens, 256))

    results = []
    for level in levels:
        t0 = time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(max_workers=level) as pool:
            futs = [
                pool.submit(call, args.base_url, args.model, 100000 + level * 1000 + i, args.max_tokens)
                for i in range(args.requests)
            ]
            rows = [f.result() for f in futs]
        elapsed = time.perf_counter() - t0
        ct = sum(r["completion"] for r in rows)
        pt = sum(r["prompt"] for r in rows)
        aggregate_tps = ct / elapsed if elapsed else 0.0
        median_latency = statistics.median(r["elapsed"] for r in rows)
        valid = sum(r["valid"] for r in rows)
        row = (level, aggregate_tps, median_latency, elapsed, pt, ct, valid)
        results.append(row)
        if not args.json:
            print(
                f"concurrency {level}: {aggregate_tps:.2f} completion tok/s aggregate | "
                f"median latency {median_latency:.2f}s | batch {elapsed:.2f}s | JSON {valid}/{len(rows)}"
            )

    best = max(results, key=lambda r: r[1])
    if args.json:
        print(json.dumps({
            "model": args.model,
            "results": [
                {
                    "concurrency": r[0],
                    "aggregate_completion_tokens_per_second": round(r[1], 6),
                    "median_latency_seconds": round(r[2], 6),
                    "batch_seconds": round(r[3], 6),
                    "prompt_tokens": r[4],
                    "completion_tokens": r[5],
                    "valid_json": r[6],
                }
                for r in results
            ],
            "best_concurrency": best[0],
            "best_aggregate_completion_tokens_per_second": round(best[1], 6),
        }))
    else:
        print()
        print(f"FASTEST CLIENT CONCURRENCY: {best[0]} ({best[1]:.2f} completion tok/s aggregate)")
        print("Use that value for Qwenomatic --max-concurrency, but do not exceed the Ollama server's OLLAMA_NUM_PARALLEL.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
