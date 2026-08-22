from __future__ import annotations

import argparse
import csv
import json
import shutil
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from looper_core.contracts import ClientLoadAccounting
from looper_core.scenario_adapters import (
    ScenarioAdapterError,
    parse_benchbase_smallbank,
    parse_dcperf_mediawiki,
    reconcile_benchbase_client_accounting,
)

from looper_benchmark_sdk.io import emit_metric, write_result

BENCHBASE_REVISION = "33c00473807ebd49304d114a6d769d2d2b2bbb34"
DCPERF_REVISION = "9308c3e3c404e0466f0a2929f15ddcf62b2215f6"


def _read_json(path: Path, label: str) -> Mapping[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ScenarioAdapterError(f"cannot read {label}: {error}") from error
    if not isinstance(document, Mapping):
        raise ScenarioAdapterError(f"{label} must contain a JSON object")
    return document


def _copy(source: Path, output: Path, name: str) -> Path:
    destination = output / name
    try:
        if source.resolve() != destination.resolve():
            shutil.copyfile(source, destination)
    except OSError as error:
        raise ScenarioAdapterError(f"cannot preserve {name}: {error}") from error
    return destination


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _emit_standard_result(
    normalized: Mapping[str, Any],
    output: Path,
    *,
    workload: str,
    artifacts: list[dict[str, str]],
    source_revision: str,
    synthetic_fixture: bool,
) -> int:
    output.mkdir(parents=True, exist_ok=True)
    metrics_path = output / "metrics.jsonl"
    metrics_path.unlink(missing_ok=True)
    for metric in normalized.get("metrics", []):
        if not isinstance(metric, Mapping):
            raise ScenarioAdapterError("normalized metrics must be objects")
        emit_metric(
            output,
            str(metric["metric"]),
            float(metric["value"]),
            str(metric["unit"]),
            workload=workload,
            sample_count=int(metric["sample_count"])
            if metric.get("sample_count") is not None
            else None,
            statistic=str(metric.get("statistic", "sample")),
            attributes={
                "adapter": normalized.get("adapter"),
                "source_revision": source_revision,
                "synthetic_fixture": synthetic_fixture,
            },
        )

    normalized_path = output / "normalized-result.json"
    _write_json(normalized_path, normalized)
    checks = []
    for check in normalized.get("checks", []):
        if not isinstance(check, Mapping):
            raise ScenarioAdapterError("normalized checks must be objects")
        checks.append(
            {
                "id": str(check["id"]),
                "passed": bool(check["passed"]),
                "scope": "block",
                "kind": str(check["kind"]),
                "message": None,
                "details": dict(check.get("details", {})),
            }
        )
    status = str(normalized.get("status", "failed"))
    write_result(
        output,
        {
            "schemaVersion": "v1alpha1",
            "status": status,
            "message": None if status == "succeeded" else "scenario adapter checks failed",
            "checks": checks,
            "artifacts": [
                {
                    "path": "normalized-result.json",
                    "role": "result",
                    "mediaType": "application/json",
                    "description": "normalized scenario result",
                },
                *artifacts,
                {
                    "path": "benchmark.log",
                    "role": "log",
                    "mediaType": "text/plain",
                    "description": "scenario adapter execution log",
                },
            ],
            "extensions": {
                "adapter": normalized.get("adapter"),
                "sourceRevision": source_revision,
                "syntheticFixture": synthetic_fixture,
                "normalizationOnly": True,
            },
        },
    )
    (output / "benchmark.log").write_text(
        "\n".join(
            [
                f"adapter={normalized.get('adapter')}",
                f"source_revision={source_revision}",
                f"workload={workload}",
                f"status={status}",
                f"synthetic_fixture={str(synthetic_fixture).lower()}",
                "normalization_only=true",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return 0 if status == "succeeded" else 2


def normalize_benchbase_smallbank(
    *,
    summary_path: Path,
    histograms_path: Path,
    latencies_path: Path,
    accounting_path: Path,
    output: Path,
    workload: str = "smallbank-postgres-serializable",
    synthetic_fixture: bool = False,
) -> int:
    summary = _read_json(summary_path, "BenchBase summary")
    histograms = _read_json(histograms_path, "BenchBase histograms")
    try:
        with latencies_path.open(encoding="utf-8", newline="") as stream:
            raw_rows = list(csv.DictReader(stream))
    except OSError as error:
        raise ScenarioAdapterError(f"cannot read BenchBase raw latency: {error}") from error
    normalized = parse_benchbase_smallbank(summary, histograms, raw_rows)
    try:
        accounting = ClientLoadAccounting.model_validate(
            _read_json(accounting_path, "client load accounting")
        )
    except ValueError as error:
        raise ScenarioAdapterError(f"invalid client load accounting: {error}") from error
    normalized = reconcile_benchbase_client_accounting(normalized, summary, accounting)
    output.mkdir(parents=True, exist_ok=True)
    _copy(summary_path, output, "summary.json")
    _copy(histograms_path, output, "transaction-histograms.json")
    _copy(latencies_path, output, "latency.raw.csv")
    _copy(accounting_path, output, "client-load-accounting.json")
    return _emit_standard_result(
        normalized,
        output,
        workload=workload,
        artifacts=[
            {
                "path": "latency.raw.csv",
                "role": "histogram",
                "mediaType": "text/csv",
                "description": "upstream per-transaction latency evidence",
            },
            {
                "path": "transaction-histograms.json",
                "role": "result",
                "mediaType": "application/json",
                "description": "upstream committed and excluded outcome counters",
            },
            {
                "path": "client-load-accounting.json",
                "role": "result",
                "mediaType": "application/json",
                "description": (
                    "planned, offered, started, completed, timeout, and headroom evidence"
                ),
            },
        ],
        source_revision=BENCHBASE_REVISION,
        synthetic_fixture=synthetic_fixture,
    )


def normalize_dcperf_mediawiki(
    *,
    result_path: Path,
    output: Path,
    workload: str = "oss_performance_mediawiki_mlp",
    synthetic_fixture: bool = False,
) -> int:
    document = _read_json(result_path, "DCPerf Benchpress result")
    normalized = parse_dcperf_mediawiki(document)
    output.mkdir(parents=True, exist_ok=True)
    _copy(result_path, output, "benchpress-result.json")
    return _emit_standard_result(
        normalized,
        output,
        workload=workload,
        artifacts=[
            {
                "path": "benchpress-result.json",
                "role": "result",
                "mediaType": "application/json",
                "description": "pinned DCPerf Benchpress output",
            }
        ],
        source_revision=DCPERF_REVISION,
        synthetic_fixture=synthetic_fixture,
    )


def _run_cli(
    parser: argparse.ArgumentParser,
    arguments: Sequence[str] | None,
    normalizer: Callable[[argparse.Namespace], int],
) -> int:
    parsed = parser.parse_args(arguments)
    try:
        return normalizer(parsed)
    except ScenarioAdapterError as error:
        output = Path(parsed.output)
        output.mkdir(parents=True, exist_ok=True)
        (output / "metrics.jsonl").unlink(missing_ok=True)
        (output / "normalized-result.json").unlink(missing_ok=True)
        (output / "benchmark.log").write_text(
            f"status=failed\nerror={error}\nnormalization_only=true\n",
            encoding="utf-8",
        )
        write_result(
            output,
            {
                "schemaVersion": "v1alpha1",
                "status": "failed",
                "message": str(error),
                "checks": [
                    {
                        "id": "scenario-normalization",
                        "passed": False,
                        "scope": "block",
                        "kind": "execution",
                        "message": str(error),
                        "details": {},
                    }
                ],
                "artifacts": [
                    {
                        "path": "benchmark.log",
                        "role": "log",
                        "mediaType": "text/plain",
                        "description": "scenario adapter failure log",
                    }
                ],
                "extensions": {"normalizationOnly": True},
            },
        )
        return 2


def benchbase_main(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Normalize pinned BenchBase SmallBank output")
    parser.add_argument("--summary", required=True)
    parser.add_argument("--histograms", required=True)
    parser.add_argument("--latencies", required=True)
    parser.add_argument("--client-accounting", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--workload", default="smallbank-postgres-serializable")
    parser.add_argument("--synthetic-fixture", action="store_true")
    return _run_cli(
        parser,
        arguments,
        lambda parsed: normalize_benchbase_smallbank(
            summary_path=Path(parsed.summary),
            histograms_path=Path(parsed.histograms),
            latencies_path=Path(parsed.latencies),
            accounting_path=Path(parsed.client_accounting),
            output=Path(parsed.output),
            workload=parsed.workload,
            synthetic_fixture=parsed.synthetic_fixture,
        ),
    )


def dcperf_main(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Normalize pinned DCPerf MediaWiki output")
    parser.add_argument("--result", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--workload", default="oss_performance_mediawiki_mlp")
    parser.add_argument("--synthetic-fixture", action="store_true")
    return _run_cli(
        parser,
        arguments,
        lambda parsed: normalize_dcperf_mediawiki(
            result_path=Path(parsed.result),
            output=Path(parsed.output),
            workload=parsed.workload,
            synthetic_fixture=parsed.synthetic_fixture,
        ),
    )
