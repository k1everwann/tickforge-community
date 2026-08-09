from __future__ import annotations

import argparse
import json
import resource
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tickforge.local_review import LlamaCppRuntime, LocalModelReviewer
from tickforge.models import Action, Bar, Decision


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def sample_bars() -> list[Bar]:
    start = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
    return [
        Bar(
            timestamp=start + timedelta(minutes=5 * index),
            open=20_000 + index * 3,
            high=20_006 + index * 3,
            low=19_996 + index * 3,
            close=20_002 + index * 3,
            volume=1_000 + index * 10,
        )
        for index in range(20)
    ]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--executable", default="llama-cli")
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument("--timeout", type=float, default=30)
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("--runs must be positive")
    if not args.model.is_file():
        parser.error("--model must point to an existing GGUF file")

    runtime = LlamaCppRuntime(args.model, args.executable)
    reviewer = LocalModelReviewer(runtime, args.timeout)
    candidate = Decision(Action.OPEN_LONG, "synthetic benchmark entry", 0.91, 20)
    bars = sample_bars()
    latencies: list[float] = []
    outcomes = {"accept": 0, "reject": 0, "fail_closed": 0}
    cpu_start = time.process_time()
    for _index in range(args.runs):
        started = time.perf_counter()
        reviewed = reviewer.review(candidate, bars)
        latencies.append(time.perf_counter() - started)
        if reviewed is candidate:
            outcomes["accept"] += 1
        elif reviewed.reason == "local model review failed closed":
            outcomes["fail_closed"] += 1
        else:
            outcomes["reject"] += 1
    cpu_seconds = time.process_time() - cpu_start
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    print(
        json.dumps(
            {
                "runs": args.runs,
                "outcomes": outcomes,
                "latency_seconds": {
                    "p50": round(percentile(latencies, 0.50), 4),
                    "p95": round(percentile(latencies, 0.95), 4),
                },
                "parent_cpu_seconds": round(cpu_seconds, 4),
                "child_cpu_seconds": round(usage.ru_utime + usage.ru_stime, 4),
                "child_max_rss_mb": round(usage.ru_maxrss / 1024, 2),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
