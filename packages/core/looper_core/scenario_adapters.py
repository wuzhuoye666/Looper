from __future__ import annotations

import csv
import json
import math
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from looper_core.analysis import quantile
from looper_core.contracts import ClientLoadAccounting


class ScenarioAdapterError(ValueError):
    pass


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ScenarioAdapterError(f"{name} must be an object")
    return value


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ScenarioAdapterError(f"{name} must be numeric")
    try:
        number = float(value)
    except ValueError as error:
        raise ScenarioAdapterError(f"{name} must be numeric") from error
    if not math.isfinite(number):
        raise ScenarioAdapterError(f"{name} must be finite")
    return number


def _integer(value: Any, name: str) -> int:
    number = _number(value, name)
    if not number.is_integer() or number < 0:
        raise ScenarioAdapterError(f"{name} must be a non-negative integer")
    return int(number)


def _metric(
    name: str,
    value: float,
    unit: str,
    statistic: str,
    *,
    sample_count: int | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "metric": name,
        "value": float(value),
        "unit": unit,
        "statistic": statistic,
    }
    if sample_count is not None:
        result["sample_count"] = sample_count
    return result


def _histogram_count(document: Mapping[str, Any], name: str) -> int:
    histogram = _mapping(document.get(name), f"histograms.{name}")
    if "NUM_SAMPLES" in histogram:
        return _integer(histogram["NUM_SAMPLES"], f"histograms.{name}.NUM_SAMPLES")
    entries = _mapping(histogram.get("HISTOGRAM"), f"histograms.{name}.HISTOGRAM")
    return sum(_integer(value, f"histograms.{name}.HISTOGRAM") for value in entries.values())


def parse_benchbase_smallbank(
    summary: Mapping[str, Any],
    histograms: Mapping[str, Any],
    raw_rows: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    benchmark_type = str(summary.get("Benchmark Type", "")).strip().lower()
    if benchmark_type != "smallbank":
        raise ScenarioAdapterError("BenchBase summary is not a SmallBank result")
    elapsed_ns = _integer(summary.get("Elapsed Time (nanoseconds)"), "elapsed nanoseconds")
    if elapsed_ns <= 0:
        raise ScenarioAdapterError("elapsed nanoseconds must be positive")
    elapsed_seconds = elapsed_ns / 1_000_000_000
    measured_requests = _integer(summary.get("Measured Requests"), "measured requests")
    attempted_tps = _number(
        summary.get("Throughput (requests/second)"), "throughput requests/second"
    )
    upstream_goodput = _number(summary.get("Goodput (requests/second)"), "goodput requests/second")

    committed = _histogram_count(histograms, "completed")
    aborted = _histogram_count(histograms, "aborted")
    retried = _histogram_count(histograms, "rejected")
    unexpected = _histogram_count(histograms, "unexpected")
    accounted_attempts = committed + aborted + retried + unexpected
    denominator = accounted_attempts or measured_requests
    if denominator <= 0:
        raise ScenarioAdapterError("BenchBase result has no accounted attempts")

    latencies_us: list[float] = []
    for index, row in enumerate(raw_rows):
        key = "Latency (microseconds)"
        if key not in row:
            raise ScenarioAdapterError(f"raw latency row {index} is missing {key!r}")
        latencies_us.append(_number(row[key], f"raw latency row {index}"))
    if not latencies_us:
        raise ScenarioAdapterError("BenchBase raw latency file contains no samples")
    latencies_ms = [value / 1000 for value in latencies_us]

    derived_attempted_tps = measured_requests / elapsed_seconds
    derived_goodput = committed / elapsed_seconds
    final_state = str(summary.get("Final State", "")).strip().upper()
    counts_reconcile = accounted_attempts == measured_requests
    throughput_reconciles = abs(derived_attempted_tps - attempted_tps) <= max(
        0.001, attempted_tps * 0.001
    )
    goodput_reconciles = abs(derived_goodput - upstream_goodput) <= max(
        0.001, upstream_goodput * 0.001
    )
    checks = [
        {
            "id": "benchbase-final-state",
            "kind": "execution",
            "passed": final_state in {"DONE", "SUCCEEDED", "COMPLETED"},
            "details": {"final_state": final_state},
        },
        {
            "id": "attempt-accounting",
            "kind": "correctness",
            "passed": counts_reconcile,
            "details": {
                "measured_requests": measured_requests,
                "accounted_attempts": accounted_attempts,
            },
        },
        {
            "id": "throughput-accounting",
            "kind": "correctness",
            "passed": throughput_reconciles,
            "details": {
                "upstream_throughput": attempted_tps,
                "derived_attempted_throughput": derived_attempted_tps,
            },
        },
        {
            "id": "goodput-accounting",
            "kind": "correctness",
            "passed": goodput_reconciles,
            "details": {
                "upstream_goodput": upstream_goodput,
                "derived_committed_goodput": derived_goodput,
            },
        },
    ]
    metrics = [
        _metric("attempted_tps", derived_attempted_tps, "transactions/second", "rate"),
        _metric("committed_tps", derived_goodput, "transactions/second", "rate"),
        _metric("committed_transactions", committed, "transactions", "count"),
        _metric("abort_ratio", aborted / denominator, "ratio", "rate"),
        _metric("retry_ratio", retried / denominator, "ratio", "rate"),
        _metric("error_ratio", unexpected / denominator, "ratio", "rate"),
        _metric(
            "latency_p50_ms",
            quantile(latencies_ms, 0.50),
            "ms",
            "p50",
            sample_count=len(latencies_ms),
        ),
        _metric(
            "latency_p95_ms",
            quantile(latencies_ms, 0.95),
            "ms",
            "p95",
            sample_count=len(latencies_ms),
        ),
        _metric(
            "latency_p99_ms",
            quantile(latencies_ms, 0.99),
            "ms",
            "p99",
            sample_count=len(latencies_ms),
        ),
        _metric(
            "latency_p999_ms",
            quantile(latencies_ms, 0.999),
            "ms",
            "p99.9",
            sample_count=len(latencies_ms),
        ),
        _metric(
            "latency_max_ms", max(latencies_ms), "ms", "maximum", sample_count=len(latencies_ms)
        ),
    ]
    return {
        "schema_version": "looper.scenario-result/v1alpha1",
        "adapter": "benchbase-smallbank-postgres",
        "upstream": "benchbase",
        "workload": "smallbank",
        "status": "succeeded" if all(check["passed"] for check in checks) else "failed",
        "metrics": metrics,
        "outcomes": {
            "committed": committed,
            "abort": aborted,
            "retry": retried,
            "error": unexpected,
            "timeout": None,
        },
        "checks": checks,
        "latency_evidence": {
            "format": "raw",
            "sample_count": len(latencies_ms),
            "source_unit": "microseconds",
        },
        "extensions": {
            "dbms_type": summary.get("DBMS Type"),
            "dbms_version": summary.get("DBMS Version"),
            "upstream_goodput": upstream_goodput,
        },
    }


def reconcile_benchbase_client_accounting(
    normalized: dict[str, Any],
    summary: Mapping[str, Any],
    accounting: ClientLoadAccounting,
) -> dict[str, Any]:
    elapsed_seconds = _integer(
        summary.get("Elapsed Time (nanoseconds)"), "elapsed nanoseconds"
    ) / 1_000_000_000
    measured_requests = _integer(summary.get("Measured Requests"), "measured requests")
    if accounting.completed_requests != measured_requests:
        raise ScenarioAdapterError(
            "client completed requests do not match BenchBase measured requests"
        )
    if abs(accounting.measurement_seconds - elapsed_seconds) > max(0.001, elapsed_seconds * 0.001):
        raise ScenarioAdapterError("client and BenchBase measurement windows do not match")

    outcomes = normalized["outcomes"]
    outcomes["timeout"] = accounting.timeout_requests
    outcome_denominator = accounting.started_requests
    metric_by_name = {metric["metric"]: metric for metric in normalized["metrics"]}
    for metric_name, outcome_name in (
        ("abort_ratio", "abort"),
        ("retry_ratio", "retry"),
        ("error_ratio", "error"),
    ):
        metric_by_name[metric_name]["value"] = outcomes[outcome_name] / outcome_denominator
    offered_load_achieved_ratio = accounting.offered_requests / (
        accounting.planned_offered_tps * accounting.measurement_seconds
    )
    normalized["metrics"].extend(
        [
            _metric(
                "offered_tps",
                accounting.planned_offered_tps,
                "transactions/second",
                "rate",
            ),
            _metric("offered_requests", accounting.offered_requests, "transactions", "count"),
            _metric("started_requests", accounting.started_requests, "transactions", "count"),
            _metric(
                "completed_requests", accounting.completed_requests, "transactions", "count"
            ),
            _metric("timeout_count", accounting.timeout_requests, "transactions", "count"),
            _metric(
                "timeout_ratio",
                accounting.timeout_requests / outcome_denominator,
                "ratio",
                "rate",
            ),
            _metric(
                "offered_load_achieved_ratio", offered_load_achieved_ratio, "ratio", "rate"
            ),
            _metric(
                "rate_limiter_lag_ratio", accounting.rate_limiter_lag_ratio, "ratio", "rate"
            ),
            _metric(
                "client_headroom_ratio", accounting.client_headroom_ratio, "ratio", "rate"
            ),
        ]
    )
    normalized["checks"].extend(
        [
            {
                "id": "client-load-accounting",
                "kind": "correctness",
                "passed": True,
                "details": accounting.model_dump(mode="json", by_alias=True),
            },
            {
                "id": "rate-limiter-lag",
                "kind": "resource",
                "passed": accounting.rate_limiter_lag_ratio < 0.01,
                "details": {"rate_limiter_lag_ratio": accounting.rate_limiter_lag_ratio},
            },
            {
                "id": "client-headroom",
                "kind": "resource",
                "passed": accounting.client_headroom_ratio >= 0.20,
                "details": {"client_headroom_ratio": accounting.client_headroom_ratio},
            },
        ]
    )
    normalized["extensions"].update(
        {
            "client_load_accounting": accounting.model_dump(mode="json", by_alias=True),
            "latency_population": "all measured attempts regardless of transaction outcome",
        }
    )
    return normalized


def load_benchbase_smallbank_fixture(directory: Path) -> dict[str, Any]:
    with (directory / "summary.json").open(encoding="utf-8") as stream:
        summary = json.load(stream)
    with (directory / "transaction-histograms.json").open(encoding="utf-8") as stream:
        histograms = json.load(stream)
    with (directory / "latency.raw.csv").open(encoding="utf-8", newline="") as stream:
        raw_rows = list(csv.DictReader(stream))
    with (directory / "client-load-accounting.json").open(encoding="utf-8") as stream:
        accounting = ClientLoadAccounting.model_validate(json.load(stream))
    normalized = parse_benchbase_smallbank(summary, histograms, raw_rows)
    return reconcile_benchbase_client_accounting(normalized, summary, accounting)


def parse_dcperf_mediawiki(document: Mapping[str, Any]) -> dict[str, Any]:
    benchmark_name = str(document.get("benchmark_name", ""))
    if benchmark_name != "oss_performance_mediawiki_mlp":
        raise ScenarioAdapterError("DCPerf result is not oss_performance_mediawiki_mlp")
    metrics = _mapping(document.get("metrics"), "metrics")
    combined = _mapping(metrics.get("Combined"), "metrics.Combined")
    requests = _integer(combined.get("Wrk requests"), "Wrk requests")
    successful = _integer(combined.get("Wrk successful requests"), "Wrk successful requests")
    failed = _integer(combined.get("Wrk failed requests"), "Wrk failed requests")
    wall_seconds = _number(combined.get("Wrk wall sec"), "Wrk wall sec")
    if requests <= 0 or wall_seconds <= 0:
        raise ScenarioAdapterError("DCPerf request count and wall time must be positive")
    upstream_rps = _number(combined.get("Wrk RPS"), "Wrk RPS")
    successful_rps = successful / wall_seconds
    failed_ratio = failed / requests
    request_difference = abs(requests - successful - failed)
    accounting_tolerance = max(1, round(requests * 0.001))

    monitor = _mapping(document.get("looper_monitor", {}), "looper_monitor")
    cpu_p95 = monitor.get("cpu_utilization_p95")
    cpu_available = isinstance(cpu_p95, (int, float)) and not isinstance(cpu_p95, bool)
    cpu_value = float(cpu_p95) if cpu_available else None
    timeouts = _integer(monitor.get("timeouts"), "looper_monitor.timeouts")
    if timeouts < 0 or timeouts > failed:
        raise ScenarioAdapterError("DCPerf timeout count must be between zero and failed requests")
    errors = failed - timeouts

    normalized_metrics = [
        _metric("closed_loop_successful_rps", successful_rps, "requests/second", "rate"),
        _metric("wrk_rps", upstream_rps, "requests/second", "rate"),
        _metric("successful_requests", successful, "requests", "count"),
        _metric("failed_request_ratio", failed_ratio, "ratio", "rate"),
        _metric("error_ratio", errors / requests, "ratio", "rate"),
        _metric("timeout_count", timeouts, "requests", "count"),
        _metric("timeout_ratio", timeouts / requests, "ratio", "rate"),
        _metric(
            "latency_p50_ms",
            _number(combined.get("Nginx P50 time"), "Nginx P50 time") * 1000,
            "ms",
            "p50",
            sample_count=successful,
        ),
        _metric(
            "latency_p95_ms",
            _number(combined.get("Nginx P95 time"), "Nginx P95 time") * 1000,
            "ms",
            "p95",
            sample_count=successful,
        ),
        _metric(
            "latency_p99_ms",
            _number(combined.get("Nginx P99 time"), "Nginx P99 time") * 1000,
            "ms",
            "p99",
            sample_count=successful,
        ),
    ]
    if cpu_value is not None:
        normalized_metrics.append(_metric("cpu_utilization_p95", cpu_value, "percent", "p95"))
    checks = [
        {
            "id": "request-accounting",
            "kind": "correctness",
            "passed": request_difference <= accounting_tolerance,
            "details": {
                "requests": requests,
                "successful": successful,
                "failed": failed,
                "difference": request_difference,
                "tolerance": accounting_tolerance,
            },
        },
        {
            "id": "cpu-saturation",
            "kind": "resource",
            "passed": cpu_value is not None and cpu_value >= 90,
            "details": {"cpu_utilization_p95": cpu_value, "minimum": 90},
        },
    ]
    return {
        "schema_version": "looper.scenario-result/v1alpha1",
        "adapter": "dcperf-mediawiki-closed-loop",
        "upstream": "dcperf",
        "workload": benchmark_name,
        "status": "succeeded" if all(check["passed"] for check in checks) else "failed",
        "metrics": normalized_metrics,
        "outcomes": {
            "committed": successful,
            "error": errors,
            "timeout": timeouts,
        },
        "checks": checks,
        "latency_evidence": {
            "format": "upstream-summary",
            "sample_count": successful,
            "available_statistics": ["p50", "p95", "p99"],
        },
        "environment": document.get("machines", []),
        "extensions": {
            "upstream_wrk_rps": upstream_rps,
            "closed_loop": True,
            "client_included_in_score": True,
        },
    }


def load_dcperf_mediawiki_fixture(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as stream:
        document = json.load(stream)
    return parse_dcperf_mediawiki(document)
