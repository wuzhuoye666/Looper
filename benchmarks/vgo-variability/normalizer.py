from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from pathlib import Path
from typing import Any


def number(row: dict[str, str], key: str) -> float | None:
    try:
        value = float(row.get(key, ""))
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def percentile(values: list[float], percentile_value: int) -> float:
    if len(values) == 1:
        return values[0]
    return statistics.quantiles(values, n=100, method="inclusive")[percentile_value - 1]


def observation(
    metric: str,
    value: float | bool,
    unit: str,
    workload: str,
    statistic: str,
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
        "attributes": {},
    }
    row.update(extra)
    return row


def main() -> int:
    parser = argparse.ArgumentParser(description="Normalize original VGO CSV evidence")
    parser.add_argument("--envelope", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    envelope = json.loads(args.envelope.read_text(encoding="utf-8"))
    workload = str(envelope["workload"]["id"])
    output = args.output.resolve()
    raw_path = output / "vgo-raw.csv"
    native_path = output / "vgo-native.json"
    metadata_path = output / "vgo-metadata.json"
    errors: list[str] = []
    rows: list[dict[str, str]] = []
    native: dict[str, Any] = {}
    try:
        with raw_path.open("r", encoding="utf-8", newline="") as stream:
            rows = list(csv.DictReader(stream))
        native = json.loads(native_path.read_text(encoding="utf-8"))
        json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as error:
        errors.append(f"could not parse original VGO evidence: {error}")

    requested = int(native.get("samplesRequested", 0)) if native else 0
    if native and native.get("schemaVersion") != "looper.vgo-native/v1":
        errors.append("the VGO native metadata schema is invalid")
    if native and native.get("workload") != workload:
        errors.append("the VGO native workload does not match the run envelope")
    if native and int(native.get("exitCode", 1)) != 0:
        errors.append("the original VGO run_case.sh did not exit successfully")

    valid_rows: list[dict[str, str]] = []
    for row in rows:
        metric = number(row, "app_metric")
        if (
            row.get("benchmark") == workload
            and row.get("phase") == "baseline"
            and row.get("condition") == "baseline"
            and row.get("exit_code") == "0"
            and row.get("correctness") == "1"
            and row.get("timeout") == "0"
            and metric is not None
            and metric > 0
        ):
            valid_rows.append(row)
    if requested < 3 or len(rows) != requested or len(valid_rows) != requested:
        errors.append(
            f"expected {requested} valid VGO samples, observed {len(valid_rows)} valid rows "
            f"out of {len(rows)} total"
        )

    metrics_path = output / "metrics.jsonl"
    if not errors:
        runtimes = [float(row["app_metric"]) for row in valid_rows]
        steal_values = [
            value for row in valid_rows if (value := number(row, "cpu_steal")) is not None
        ]
        mean_runtime = statistics.fmean(runtimes)
        runtime_sd = statistics.stdev(runtimes)
        runtime_cv = runtime_sd / mean_runtime
        summary = {
            "sampleCount": len(runtimes),
            "meanRuntimeSeconds": mean_runtime,
            "medianRuntimeSeconds": statistics.median(runtimes),
            "p95RuntimeSeconds": percentile(runtimes, 95),
            "runtimeSdSeconds": runtime_sd,
            "runtimeCv": runtime_cv,
            "correctnessRate": len(valid_rows) / len(rows),
            "cpuStealP95Percent": percentile(steal_values, 95) if steal_values else 0.0,
        }
        metric_rows = [
            observation("run_ok", True, "bool", workload, "boolean"),
            observation("sample_count", float(summary["sampleCount"]), "count", workload, "count"),
            observation(
                "median_runtime_seconds",
                summary["medianRuntimeSeconds"],
                "s",
                workload,
                "median",
                sampleCount=len(runtimes),
            ),
            observation(
                "p95_runtime_seconds",
                summary["p95RuntimeSeconds"],
                "s",
                workload,
                "p95",
                sampleCount=len(runtimes),
            ),
            observation(
                "runtime_cv",
                summary["runtimeCv"],
                "ratio",
                workload,
                "rate",
                sampleCount=len(runtimes),
            ),
            observation(
                "correctness_rate",
                summary["correctnessRate"],
                "ratio",
                workload,
                "rate",
                sampleCount=len(rows),
            ),
            observation(
                "cpu_steal_p95_percent",
                summary["cpuStealP95Percent"],
                "%",
                workload,
                "p95",
                sampleCount=len(steal_values) or 1,
            ),
        ]
        metric_rows.extend(
            observation(
                "runtime_seconds",
                value,
                "s",
                workload,
                "sample",
                sampleIndex=index,
                sampleCount=len(runtimes),
            )
            for index, value in enumerate(runtimes)
        )
        metrics_path.write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in metric_rows),
            encoding="utf-8",
        )
    else:
        summary = {}
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
                "message": "the bundled original scripts/run_case.sh completed"
                if bool(native) and int(native.get("exitCode", 1)) == 0
                else "the original VGO script did not complete",
                "details": {"entryPoint": native.get("originalEntryPoint") if native else None},
            },
            {
                "id": "vgo-sample-contract",
                "passed": succeeded,
                "scope": "attempt",
                "kind": "correctness",
                "message": "all requested rows passed exit, timeout and correctness gates"
                if succeeded
                else "; ".join(errors),
                "details": {"requested": requested, "valid": len(valid_rows), "total": len(rows)},
            },
        ],
        "artifacts": [
            {
                "path": "vgo-raw.csv",
                "role": "raw-result",
                "mediaType": "text/csv",
                "description": "Original VGO run_case.py baseline CSV",
            },
            {
                "path": "vgo-native.json",
                "role": "profile",
                "mediaType": "application/json",
                "description": "Looper-to-VGO execution metadata",
            },
            {
                "path": "vgo-metadata.json",
                "role": "profile",
                "mediaType": "application/json",
                "description": "Original VGO immutable phase metadata",
            },
            {
                "path": "vgo-run.log",
                "role": "log",
                "mediaType": "text/plain",
                "description": "Adapter and original VGO phase logs",
            },
        ],
        "extensions": {
            "nativeSchema": "looper.vgo-native/v1",
            "sourceDigest": native.get("sourceDigest") if native else None,
            "summary": summary,
        },
    }
    (output / "result.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return 0 if succeeded else 2


if __name__ == "__main__":
    raise SystemExit(main())
