#!/usr/bin/env python3
"""Fail closed when DCPerf output does not meet the Adapter contract."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REQUIRED_METRICS = {
    "closed_loop_successful_rps",
    "wrk_rps",
    "successful_requests",
    "failed_request_ratio",
    "error_ratio",
    "timeout_count",
    "timeout_ratio",
    "latency_p50_ms",
    "latency_p95_ms",
    "latency_p99_ms",
    "cpu_utilization_p95",
}
REQUIRED_CHECKS = {
    "native-identity",
    "request-accounting",
    "failure-budget",
    "timeout-budget",
    "tail-sample-count",
    "cpu-saturation",
}
REQUIRED_ARTIFACTS = {
    "benchpress-result.json",
    "native-result-enriched.json",
    "native-system-specs.json",
    "native-run.json",
    "benchmark.log",
    "profile-status.txt",
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    output = arguments.output.resolve()
    try:
        result = json.loads((output / "result.json").read_text(encoding="utf-8"))
        if result.get("schemaVersion") != "v1alpha1":
            raise ValueError("result schemaVersion must be v1alpha1")
        lines = [
            json.loads(line)
            for line in (output / "metrics.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        names = {item.get("metric") for item in lines}
        missing = sorted(REQUIRED_METRICS - names)
        if missing:
            raise ValueError(f"missing normalized metrics: {missing}")
        missing_artifacts = sorted(
            name for name in REQUIRED_ARTIFACTS if not (output / name).is_file()
        )
        if missing_artifacts:
            raise ValueError(f"missing required native evidence: {missing_artifacts}")
        check_ids = {item.get("id") for item in result.get("checks", []) if isinstance(item, dict)}
        missing_checks = sorted(REQUIRED_CHECKS - check_ids)
        if missing_checks:
            raise ValueError(f"missing required result checks: {missing_checks}")
        if result.get("status") != "succeeded":
            raise ValueError(result.get("message") or "normalized result failed a suite gate")
        checks = result.get("checks")
        if not isinstance(checks, list) or not checks or not all(
            isinstance(item, dict) and item.get("passed") is True for item in checks
        ):
            raise ValueError("normalized result contains a failed check")
        print("[dcperf-validate] result contract passed", flush=True)
        return 0
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as error:
        print(f"[dcperf-validate] ERROR: {error}", file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
