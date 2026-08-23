#!/usr/bin/env python3
"""Normalize DCPerf's native Benchpress JSON into Looper Adapter contracts."""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

JOB_NAME = "oss_performance_mediawiki_mlp"
SOURCE_REVISION = "9308c3e3c404e0466f0a2929f15ddcf62b2215f6"


class NormalizerError(RuntimeError):
    pass


def now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise NormalizerError(f"cannot read native result {path}: {error}") from error
    if not isinstance(value, dict):
        raise NormalizerError("native result must be a JSON object")
    return value


def number(value: Any, name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise NormalizerError(f"native metric {name} must be a finite number")
    return float(value)


def integer(value: Any, name: str) -> int:
    parsed = number(value, name)
    if parsed < 0 or parsed != int(parsed):
        raise NormalizerError(f"native metric {name} must be a non-negative integer")
    return int(parsed)


def metric_line(
    metric: str,
    value: float | bool,
    unit: str,
    *,
    statistic: str,
    sample_count: int | None = None,
    attributes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schemaVersion": "v1alpha1",
        "metric": metric,
        "value": value,
        "unit": unit,
        "phase": "measurement",
        "workload": JOB_NAME,
        "statistic": statistic,
        "timestamp": now(),
    }
    if sample_count is not None:
        result["sampleCount"] = sample_count
    if attributes:
        result["attributes"] = attributes
    return result


def check(
    check_id: str,
    kind: str,
    passed: bool,
    details: dict[str, Any],
    message: str,
) -> dict[str, Any]:
    return {
        "id": check_id,
        "scope": "attempt",
        "kind": kind,
        "passed": bool(passed),
        "message": message,
        "details": details,
    }


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def failed_result(output: Path, message: str) -> None:
    write_json(
        output / "result.json",
        {
            "schemaVersion": "v1alpha1",
            "status": "failed",
            "message": message,
            "checks": [
                check("native-result-readable", "execution", False, {}, message),
            ],
            "extensions": {
                "adapter": "dcperf-mediawiki-closed-loop",
                "sourceRevision": SOURCE_REVISION,
            },
        },
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--envelope", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    output = arguments.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    try:
        native_path = output / "native-result-enriched.json"
        if not native_path.is_file():
            native_path = output / "benchpress-result.json"
        native = read_object(native_path)
        native_run_path = output / "native-run.json"
        native_run = read_object(native_run_path) if native_run_path.is_file() else {}
        run_parameters = native_run.get("parameters")
        profile_requested = (
            bool(run_parameters.get("profile"))
            if isinstance(run_parameters, dict) and isinstance(run_parameters.get("profile"), bool)
            else True
        )
        if native.get("benchmark_name") != JOB_NAME:
            raise NormalizerError(
                f"native benchmark_name must be {JOB_NAME}, got {native.get('benchmark_name')!r}"
            )
        metrics = native.get("metrics")
        if not isinstance(metrics, dict) or not isinstance(metrics.get("Combined"), dict):
            raise NormalizerError("native result is missing metrics.Combined")
        combined = metrics["Combined"]
        requests = integer(combined.get("Wrk requests"), "Wrk requests")
        successful = integer(combined.get("Wrk successful requests"), "Wrk successful requests")
        failed = integer(combined.get("Wrk failed requests"), "Wrk failed requests")
        wall_seconds = number(combined.get("Wrk wall sec"), "Wrk wall sec")
        upstream_rps = number(combined.get("Wrk RPS"), "Wrk RPS")
        if requests <= 0 or wall_seconds <= 0:
            raise NormalizerError("native request count and wall time must be positive")
        if successful + failed != requests:
            accounting_difference = abs(requests - successful - failed)
        else:
            accounting_difference = 0
        monitor = native.get("looper_monitor")
        if not isinstance(monitor, dict):
            raise NormalizerError("native result is missing looper_monitor evidence")
        timeouts = integer(monitor.get("timeouts", 0), "looper_monitor.timeouts")
        if timeouts > failed:
            raise NormalizerError("timeout count cannot exceed failed requests")
        cpu_p95 = number(monitor.get("cpu_utilization_p95"), "looper_monitor.cpu_utilization_p95")
        errors = failed - timeouts
        failed_ratio = failed / requests
        error_ratio = errors / requests
        timeout_ratio = timeouts / requests
        latency_values: dict[str, float] = {}
        for suffix, native_key in (
            ("p50", "Nginx P50 time"),
            ("p95", "Nginx P95 time"),
            ("p99", "Nginx P99 time"),
        ):
            latency_values[suffix] = number(combined.get(native_key), native_key) * 1000.0
        successful_rps = successful / wall_seconds
        tolerance = max(1, round(requests * 0.001))
        checks = [
            check(
                "native-identity",
                "execution",
                True,
                {"benchmarkName": native.get("benchmark_name"), "sourceRevision": SOURCE_REVISION},
                "native DCPerf workload identity is present",
            ),
            check(
                "request-accounting",
                "correctness",
                accounting_difference <= tolerance,
                {
                    "requests": requests,
                    "successful": successful,
                    "failed": failed,
                    "difference": accounting_difference,
                    "tolerance": tolerance,
                },
                "Wrk requests reconcile to successful plus failed requests",
            ),
            check(
                "failure-budget",
                "slo",
                failed_ratio < 0.01,
                {"failedRequestRatio": failed_ratio, "maximum": 0.01},
                "failed request ratio is below the suite gate",
            ),
            check(
                "timeout-budget",
                "slo",
                timeout_ratio <= 0.001,
                {"timeoutRatio": timeout_ratio, "maximum": 0.001},
                "timeout request ratio is within the closed-loop budget",
            ),
            check(
                "tail-sample-count",
                "statistical",
                successful >= 100000,
                {"successfulRequests": successful, "minimum": 100000},
                "native latency summary has the required request population",
            ),
            check(
                "cpu-saturation",
                "resource",
                cpu_p95 >= 90.0,
                {"cpuUtilizationP95": cpu_p95, "minimum": 90.0},
                "target CPU reaches the DCPerf saturation floor",
            ),
        ]
        sample_count = max(1, successful)
        observations = [
            metric_line(
                "closed_loop_successful_rps",
                successful_rps,
                "requests/second",
                statistic="rate",
                sample_count=sample_count,
            ),
            metric_line(
                "wrk_rps",
                upstream_rps,
                "requests/second",
                statistic="rate",
                sample_count=sample_count,
            ),
            metric_line(
                "successful_requests",
                float(successful),
                "requests",
                statistic="count",
                sample_count=sample_count,
            ),
            metric_line(
                "failed_request_ratio",
                failed_ratio,
                "ratio",
                statistic="mean",
                sample_count=sample_count,
            ),
            metric_line(
                "error_ratio", error_ratio, "ratio", statistic="mean", sample_count=sample_count
            ),
            metric_line(
                "timeout_count",
                float(timeouts),
                "requests",
                statistic="count",
                sample_count=sample_count,
            ),
            metric_line(
                "timeout_ratio", timeout_ratio, "ratio", statistic="mean", sample_count=sample_count
            ),
            metric_line(
                "latency_p50_ms",
                latency_values["p50"],
                "ms",
                statistic="p50",
                sample_count=sample_count,
            ),
            metric_line(
                "latency_p95_ms",
                latency_values["p95"],
                "ms",
                statistic="p95",
                sample_count=sample_count,
            ),
            metric_line(
                "latency_p99_ms",
                latency_values["p99"],
                "ms",
                statistic="p99",
                sample_count=sample_count,
            ),
            metric_line(
                "cpu_utilization_p95",
                cpu_p95,
                "percent",
                statistic="p95",
                sample_count=sample_count,
            ),
        ]
        (output / "metrics.jsonl").write_text(
            "".join(json.dumps(item, sort_keys=True) + "\n" for item in observations),
            encoding="utf-8",
        )
        passed = all(item["passed"] for item in checks)
        result = {
            "schemaVersion": "v1alpha1",
            "status": "succeeded" if passed else "failed",
            "message": None
            if passed
            else "one or more DCPerf correctness or resource gates failed",
            "checks": checks,
            "extensions": {
                "adapter": "dcperf-mediawiki-closed-loop",
                "upstream": "dcperf",
                "sourceRevision": SOURCE_REVISION,
                "workload": JOB_NAME,
                "closedLoop": True,
                "clientIncludedInScore": True,
                "outcomes": {"committed": successful, "error": errors, "timeout": timeouts},
                "requestAccounting": {
                    "requests": requests,
                    "successful": successful,
                    "failed": failed,
                    "difference": accounting_difference,
                },
                "latencyEvidence": {
                    "format": "upstream-summary",
                    "sampleCount": sample_count,
                    "statistics": ["p50", "p95", "p99"],
                    "unit": "ms",
                },
                "nativeResult": "benchpress-result.json",
                "nativeSystemSpecs": "native-system-specs.json",
            },
        }
        write_json(output / "result.json", result)
        profile_present = (output / "perf.data").is_file()
        (output / "profile-status.txt").write_text(
            f"profile_requested={'true' if profile_requested else 'false'}\n"
            + f"perf_data_present={'true' if profile_present else 'false'}\n"
            + "profile_source=DCPerf packages/mediawiki/perf-record.sh\n",
            encoding="utf-8",
        )
        return 0
    except (OSError, KeyError, TypeError, ValueError, NormalizerError) as error:
        failed_result(output, str(error))
        print(f"[dcperf-normalizer] ERROR: {error}", file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
