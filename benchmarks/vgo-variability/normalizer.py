from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from pathlib import Path
from typing import Any

CATEGORICAL_COLUMNS = {
    "run_id",
    "benchmark",
    "phase",
    "condition",
    "timestamp",
    "app_metric_name",
    "thp_state",
    "log_file",
}


def number(row: dict[str, str], key: str) -> float | None:
    try:
        value = float(row.get(key, ""))
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def measurement_value(row: dict[str, str], workload: str) -> float | None:
    """Return the duration used by the cross-condition variability analysis.

    p7zip prints ``Avr:`` with CPU usage as its first numeric column.  The
    vendored VGO runner historically stored that constant (usually 100) in
    ``app_metric``.  Its separately measured wall clock duration is the real
    comparable timing signal and is already retained in every raw CSV row.
    """

    return number(row, "wall_time_s") if workload == "7z" else number(row, "app_metric")


def percentile(values: list[float], percentile_value: int) -> float:
    if len(values) == 1:
        return values[0]
    return statistics.quantiles(values, n=100, method="inclusive")[percentile_value - 1]


def describe(values: list[float]) -> dict[str, float | int]:
    mean = statistics.fmean(values)
    sd = statistics.stdev(values) if len(values) > 1 else 0.0
    return {
        "count": len(values),
        "minimum": min(values),
        "maximum": max(values),
        "mean": mean,
        "median": statistics.median(values),
        "p95": percentile(values, 95),
        "standardDeviation": sd,
        "coefficientOfVariation": sd / mean if mean else 0.0,
    }


def observation(
    metric: str,
    value: float | bool,
    unit: str,
    workload: str,
    statistic: str,
    *,
    attributes: dict[str, Any] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    row = {
        "schemaVersion": "v1alpha1",
        "metric": metric,
        "value": value,
        "unit": unit,
        "phase": "measurement",
        "workload": workload,
        "statistic": statistic,
        "attributes": attributes or {},
    }
    row.update(extra)
    return row


def valid(row: dict[str, str], workload: str) -> bool:
    metric = measurement_value(row, workload)
    return (
        row.get("benchmark") == workload
        and row.get("exit_code") == "0"
        and row.get("correctness") == "1"
        and row.get("timeout") == "0"
        and metric is not None
        and metric > 0
    )


def profile_diagnostics(rows: list[dict[str, str]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    if not rows:
        return result
    for column in rows[0]:
        if column in CATEGORICAL_COLUMNS:
            continue
        values = [value for row in rows if (value := number(row, column)) is not None]
        if values:
            result[column] = describe(values)
    result["categorical"] = {
        column: sorted({row.get(column, "") for row in rows})
        for column in sorted(CATEGORICAL_COLUMNS & set(rows[0]))
    }
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Normalize minimal complete VGO diagnosis evidence"
    )
    parser.add_argument("--envelope", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    envelope = json.loads(args.envelope.read_text(encoding="utf-8"))
    workload = str(envelope["workload"]["id"])
    output = args.output.resolve()
    errors: list[str] = []
    rows: list[dict[str, str]] = []
    native: dict[str, Any] = {}
    metadata: dict[str, Any] = {}
    try:
        with (output / "vgo-raw.csv").open("r", encoding="utf-8", newline="") as stream:
            rows = list(csv.DictReader(stream))
        native = json.loads((output / "vgo-native.json").read_text(encoding="utf-8"))
        metadata = json.loads((output / "vgo-metadata.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as error:
        errors.append(f"could not parse original VGO evidence: {error}")

    if native and native.get("schemaVersion") != "looper.vgo-native/v2":
        errors.append("the VGO native metadata schema is invalid")
    if native and native.get("workload") != workload:
        errors.append("the VGO native workload does not match the run envelope")
    if native and int(native.get("exitCode", 1)) != 0:
        errors.append("the original VGO phase plan did not exit successfully")

    requested = native.get("requestedCounts") or {}
    order_plan = native.get("alternatingOrder", [])
    order_values = [item.get("order") for item in order_plan if isinstance(item, dict)]
    expected_blocks = int(requested.get("blocks", 0))
    plan_ok = (
        len(order_plan) == expected_blocks
        and [item.get("block") for item in order_plan] == list(range(1, expected_blocks + 1))
        and set(order_values) <= {"baseline-first", "mitigated-first"}
        and abs(order_values.count("baseline-first") - order_values.count("mitigated-first")) <= 1
        and all(
            item.get("runsPerCondition") == requested.get("perConditionPerBlock")
            for item in order_plan
        )
    )
    if not plan_ok:
        errors.append("the VGO randomized alternating block plan is invalid")
    groups = {
        "profile": [
            row
            for row in rows
            if row.get("phase") == "profile" and row.get("condition") == "baseline"
        ],
        "baseline": [
            row
            for row in rows
            if row.get("phase") == "blocked" and row.get("condition") == "baseline"
        ],
        "mitigated": [
            row
            for row in rows
            if row.get("phase") == "blocked" and row.get("condition") == "mitigated"
        ],
        "rollback": [
            row
            for row in rows
            if row.get("phase") == "rollback" and row.get("condition") == "baseline"
        ],
    }
    valid_groups = {
        name: [row for row in group if valid(row, workload)] for name, group in groups.items()
    }
    for name in groups:
        expected = int(requested.get(name, 0))
        if expected < 3 or len(groups[name]) != expected or len(valid_groups[name]) != expected:
            errors.append(
                f"{name}: expected {expected} valid rows, observed {len(valid_groups[name])} "
                f"valid rows out of {len(groups[name])}"
            )

    phase_statistics: dict[str, Any] = {}
    for name, group in valid_groups.items():
        values = [value for row in group if (value := measurement_value(row, workload)) is not None]
        if values:
            phase_statistics[name] = describe(values)

    profile_rows = valid_groups["profile"]
    diagnostics = {
        "schemaVersion": "looper.vgo-diagnostics/v1",
        "workload": workload,
        "parameters": native.get("parameters", {}),
        "formalReferenceCounts": native.get("formalReferenceCounts", {}),
        "requestedCounts": requested,
        "alternatingOrder": native.get("alternatingOrder", []),
        "mitigation": native.get("mitigation", {}),
        "machineGate": native.get("machineGate"),
        "phaseStatistics": phase_statistics,
        "profileParameters": profile_diagnostics(profile_rows),
        "phaseMetadata": metadata,
    }

    metric_rows: list[dict[str, Any]] = []
    comparison: dict[str, Any] = {}
    if not errors:
        baseline = phase_statistics["baseline"]
        mitigated = phase_statistics["mitigated"]
        rollback = phase_statistics["rollback"]
        baseline_cv = float(baseline["coefficientOfVariation"])
        mitigated_cv = float(mitigated["coefficientOfVariation"])
        baseline_median = float(baseline["median"])
        mitigated_median = float(mitigated["median"])
        comparison = {
            "cvRatio": mitigated_cv / baseline_cv if baseline_cv else 0.0,
            "cvReductionRatio": (baseline_cv - mitigated_cv) / baseline_cv if baseline_cv else 0.0,
            "medianImprovementRatio": (baseline_median - mitigated_median) / baseline_median,
            "p95ImprovementRatio": (float(baseline["p95"]) - float(mitigated["p95"]))
            / float(baseline["p95"]),
            "rollbackMedianDriftRatio": abs(float(rollback["median"]) / baseline_median - 1.0),
        }
        diagnostics["comparison"] = comparison
        metric_rows = [
            observation("run_ok", True, "bool", workload, "boolean"),
            observation(
                "sample_count",
                float(sum(len(group) for group in valid_groups.values())),
                "count",
                workload,
                "count",
            ),
            observation(
                "runtime_cv",
                baseline_cv,
                "ratio",
                workload,
                "rate",
                attributes={"condition": "baseline"},
                sampleCount=int(baseline["count"]),
            ),
            observation(
                "optimized_runtime_cv",
                mitigated_cv,
                "ratio",
                workload,
                "rate",
                attributes={"condition": "mitigated"},
                sampleCount=int(mitigated["count"]),
            ),
            observation(
                "cv_reduction_ratio", comparison["cvReductionRatio"], "ratio", workload, "rate"
            ),
            observation(
                "median_runtime_seconds",
                baseline_median,
                "s",
                workload,
                "median",
                attributes={"condition": "baseline"},
                sampleCount=int(baseline["count"]),
            ),
            observation(
                "optimized_median_runtime_seconds",
                mitigated_median,
                "s",
                workload,
                "median",
                attributes={"condition": "mitigated"},
                sampleCount=int(mitigated["count"]),
            ),
            observation(
                "median_improvement_ratio",
                comparison["medianImprovementRatio"],
                "ratio",
                workload,
                "rate",
            ),
            observation(
                "p95_runtime_seconds",
                float(baseline["p95"]),
                "s",
                workload,
                "p95",
                attributes={"condition": "baseline"},
                sampleCount=int(baseline["count"]),
            ),
            observation(
                "optimized_p95_runtime_seconds",
                float(mitigated["p95"]),
                "s",
                workload,
                "p95",
                attributes={"condition": "mitigated"},
                sampleCount=int(mitigated["count"]),
            ),
            observation(
                "p95_improvement_ratio",
                comparison["p95ImprovementRatio"],
                "ratio",
                workload,
                "rate",
            ),
            observation(
                "rollback_median_drift_ratio",
                comparison["rollbackMedianDriftRatio"],
                "ratio",
                workload,
                "rate",
                sampleCount=int(rollback["count"]),
            ),
            observation("correctness_rate", 1.0, "ratio", workload, "rate", sampleCount=len(rows)),
            observation(
                "cpu_steal_p95_percent",
                percentile(
                    [value for row in rows if (value := number(row, "cpu_steal")) is not None], 95
                ),
                "%",
                workload,
                "p95",
                sampleCount=len(rows),
            ),
        ]
        sample_offset = 0
        for condition in ("baseline", "mitigated"):
            values = [
                value
                for row in valid_groups[condition]
                if (value := measurement_value(row, workload)) is not None
            ]
            metric_rows.extend(
                observation(
                    "runtime_seconds",
                    value,
                    "s",
                    workload,
                    "sample",
                    attributes={"condition": condition, "design": "randomized-blocked-ab"},
                    sampleIndex=sample_offset + index,
                    sampleCount=len(values),
                )
                for index, value in enumerate(values)
            )
            sample_offset += len(values)

    (output / "vgo-diagnostics.json").write_text(
        json.dumps(diagnostics, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    metrics_path = output / "metrics.jsonl"
    if not errors:
        metrics_path.write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in metric_rows),
            encoding="utf-8",
        )
    else:
        metrics_path.unlink(missing_ok=True)

    succeeded = not errors
    result = {
        "schemaVersion": "v1alpha1",
        "status": "succeeded" if succeeded else "failed",
        "message": None if succeeded else "; ".join(errors),
        "checks": [
            {
                "id": "original-vgo-script",
                "passed": bool(native) and int(native.get("exitCode", 1)) == 0,
                "scope": "attempt",
                "kind": "execution",
                "message": "all original VGO run_case.sh phases completed",
                "details": {"entryPoint": native.get("originalEntryPoint") if native else None},
            },
            {
                "id": "vgo-sample-contract",
                "passed": succeeded,
                "scope": "attempt",
                "kind": "correctness",
                "message": "all profile, A/B and rollback rows passed gates"
                if succeeded
                else "; ".join(errors),
                "details": {
                    name: {
                        "requested": requested.get(name),
                        "valid": len(valid_groups[name]),
                        "total": len(groups[name]),
                    }
                    for name in groups
                },
            },
            {
                "id": "vgo-alternating-plan",
                "passed": plan_ok,
                "scope": "attempt",
                "kind": "statistical",
                "message": (
                    "baseline and mitigation used a deterministic balanced randomized block order"
                ),
                "details": {
                    "blocks": requested.get("blocks"),
                    "order": native.get("alternatingOrder", []),
                },
            },
        ],
        "artifacts": [
            {
                "path": "vgo-raw.csv",
                "role": "raw-result",
                "mediaType": "text/csv",
                "description": "All original VGO profile, blocked A/B and rollback rows",
            },
            {
                "path": "vgo-native.json",
                "role": "profile",
                "mediaType": "application/json",
                "description": "Complete parameters, commands, mitigation and randomized order",
            },
            {
                "path": "vgo-metadata.json",
                "role": "profile",
                "mediaType": "application/json",
                "description": "Original immutable metadata for every phase and condition",
            },
            {
                "path": "vgo-diagnostics.json",
                "role": "result",
                "mediaType": "application/json",
                "description": (
                    "All available raw parameters summarized by phase, plus "
                    "baseline/optimization comparison"
                ),
            },
            {
                "path": "vgo-run.log",
                "role": "log",
                "mediaType": "text/plain",
                "description": "Every adapter command and original VGO phase log",
            },
        ],
        "extensions": {
            "nativeSchema": "looper.vgo-native/v2",
            "sourceDigest": native.get("sourceDigest") if native else None,
            "parameters": native.get("parameters", {}),
            "executionPlan": requested,
            "alternatingOrder": native.get("alternatingOrder", []),
            "mitigation": native.get("mitigation", {}),
            "phaseStatistics": phase_statistics,
            "comparison": comparison,
            "profileDiagnostics": diagnostics.get("profileParameters", {}),
        },
    }
    (output / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return 0 if succeeded else 2


if __name__ == "__main__":
    raise SystemExit(main())
