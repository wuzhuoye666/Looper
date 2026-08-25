from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any


def _metric(
    name: str,
    value: float,
    unit: str,
    *,
    statistic: str,
    samples: int = 1,
) -> dict[str, Any]:
    return {
        "schemaVersion": "v1alpha1",
        "metric": name,
        "value": value,
        "unit": unit,
        "phase": "measurement",
        "workload": "business-iteration",
        "statistic": statistic,
        "sampleCount": samples,
        "attributes": {},
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--envelope", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    json.loads(args.envelope.read_text(encoding="utf-8"))
    native_path = args.output / "capacity-native.json"
    try:
        native = json.loads(native_path.read_text(encoding="utf-8"))
        offered = int(native["offeredRequests"])
        started = int(native["startedRequests"])
        completed = int(native["completedRequests"])
        timeouts = int(native["timeoutRequests"])
        success = int(native["successRequests"])
        errors = int(native["errorRequests"])
        samples = int(native["latency"]["samples"])
        elapsed = float(native["elapsedSeconds"])
        valid = all(
            (
                native.get("schemaVersion") == "looper.http-capacity/v1",
                offered > 0,
                0 <= started <= offered,
                completed + timeouts == started,
                success + errors + timeouts == started,
                samples == started,
                elapsed > 0,
            )
        )
        message = None if valid else "client accounting invariants did not close"
    except (OSError, ValueError, KeyError, TypeError) as error:
        native = {}
        valid = False
        message = f"capacity evidence is invalid: {error}"
    args.output.mkdir(parents=True, exist_ok=True)
    metrics_path = args.output / "metrics.jsonl"
    metrics_path.unlink(missing_ok=True)
    if valid:
        success_rate = success / started if started else 0.0
        error_ratio = errors / started if started else 1.0
        timeout_ratio = timeouts / started if started else 1.0
        values = [
            _metric(
                "offered_tps",
                float(native["offeredRps"]),
                "iterations/second",
                statistic="rate",
            ),
            _metric("attempted_tps", started / elapsed, "iterations/second", statistic="rate"),
            _metric("committed_tps", success / elapsed, "iterations/second", statistic="rate"),
            _metric("offered_requests", offered, "iterations", statistic="count"),
            _metric("started_requests", started, "iterations", statistic="count"),
            _metric("completed_requests", completed, "iterations", statistic="count"),
            _metric("success_rate", success_rate, "ratio", statistic="rate"),
            _metric("error_ratio", error_ratio, "ratio", statistic="rate"),
            _metric("abort_ratio", 0.0, "ratio", statistic="rate"),
            _metric("timeout_ratio", timeout_ratio, "ratio", statistic="rate"),
            _metric(
                "offered_load_achieved_ratio",
                started / offered,
                "ratio",
                statistic="rate",
            ),
            _metric(
                "rate_limiter_lag_ratio",
                float(native["rateLimiterLagRatio"]),
                "ratio",
                statistic="rate",
            ),
            _metric(
                "client_headroom_ratio",
                float(native["clientHeadroomRatio"]),
                "ratio",
                statistic="rate",
            ),
            _metric(
                "latency_p50_ms",
                float(native["latency"]["p50Ms"]),
                "ms",
                statistic="p50",
                samples=samples,
            ),
            _metric(
                "latency_p95_ms",
                float(native["latency"]["p95Ms"]),
                "ms",
                statistic="p95",
                samples=samples,
            ),
            _metric(
                "latency_p99_ms",
                float(native["latency"]["p99Ms"]),
                "ms",
                statistic="p99",
                samples=samples,
            ),
            _metric(
                "latency_p999_ms",
                float(native["latency"]["p999Ms"]),
                "ms",
                statistic="p99.9",
                samples=samples,
            ),
            _metric(
                "latency_max_ms",
                float(native["latency"]["maxMs"]),
                "ms",
                statistic="maximum",
                samples=samples,
            ),
        ]
        if not all(math.isfinite(float(item["value"])) for item in values):
            valid = False
            message = "capacity evidence contains non-finite metrics"
        else:
            metrics_path.write_text(
                "".join(json.dumps(item, sort_keys=True) + "\n" for item in values),
                encoding="utf-8",
            )
    semantic_passed = valid and int(native.get("semanticFailures", 1)) == 0
    resource_passed = valid and float(native.get("rateLimiterLagRatio", 1)) < 0.01 and float(
        native.get("clientHeadroomRatio", 0)
    ) >= 0.20 and int(native.get("startedRequests", 0)) / max(
        1, int(native.get("offeredRequests", 1))
    ) >= 0.99
    result = {
        "schemaVersion": "v1alpha1",
        "status": "succeeded" if valid else "failed",
        "message": message,
        "checks": [
            {
                "id": "business-response-correctness",
                "passed": semantic_passed,
                "scope": "block",
                "kind": "correctness",
                "message": (
                    "all semantic response assertions passed"
                    if semantic_passed
                    else "a semantic response assertion failed"
                ),
                "details": {"semanticFailures": native.get("semanticFailures")},
            },
            {
                "id": "load-generator-validity",
                "passed": resource_passed,
                "scope": "block",
                "kind": "resource",
                "message": (
                    "client offered-load accounting and headroom are valid"
                    if resource_passed
                    else "load generator could not sustain valid offered load"
                ),
                "details": {
                    "lagRatio": native.get("rateLimiterLagRatio"),
                    "headroomRatio": native.get("clientHeadroomRatio"),
                },
            },
        ],
        "artifacts": [
            {
                "path": "capacity-native.json",
                "role": "raw-result",
                "mediaType": "application/json",
                "description": "native HTTP capacity counters and per-step tail evidence",
            },
            {
                "path": "benchmark.log",
                "role": "log",
                "mediaType": "text/plain",
                "description": "capacity normalizer summary",
            },
        ],
        "extensions": {"nativeSchema": "looper.http-capacity/v1", "synthetic": False},
    }
    (args.output / "result.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (args.output / "benchmark.log").write_text(
        f"status={result['status']}\ntarget={native.get('targetId', 'unknown')}\nsynthetic=false\n",
        encoding="utf-8",
    )
    return 0 if valid else 2


if __name__ == "__main__":
    raise SystemExit(main())
