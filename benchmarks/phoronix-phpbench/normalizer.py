"""Normalize the pinned PTS PHPBench JSON export into Looper observations."""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from looper_benchmark_sdk import emit_metric, load_envelope, write_result

PINNED_PROFILE = "pts/phpbench-1.1.6"
EXPECTED_SCALE = "Score"
EXPECTED_PROPORTION = "HIB"


class NormalizationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PhpBenchResult:
    score: float
    raw_scores: tuple[float, ...]
    profile: str
    scale: str
    proportion: str


def _finite_number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise NormalizationError(f"{label} must be numeric")
    number = float(value)
    if not math.isfinite(number):
        raise NormalizationError(f"{label} must be finite")
    return number


def parse_pts_result(document: dict[str, Any]) -> PhpBenchResult:
    result_objects = document.get("results")
    if not isinstance(result_objects, dict) or len(result_objects) != 1:
        raise NormalizationError("expected exactly one PTS result object")
    result = next(iter(result_objects.values()))
    if not isinstance(result, dict):
        raise NormalizationError("PTS result object must be an object")

    profile = str(result.get("identifier", ""))
    scale = str(result.get("scale", ""))
    proportion = str(result.get("proportion", ""))
    if profile != PINNED_PROFILE:
        raise NormalizationError(
            f"profile mismatch: expected {PINNED_PROFILE}, received {profile or '<missing>'}"
        )
    if scale.casefold() != EXPECTED_SCALE.casefold():
        raise NormalizationError(
            f"result scale mismatch: expected {EXPECTED_SCALE}, received {scale or '<missing>'}"
        )
    if proportion.upper() != EXPECTED_PROPORTION:
        raise NormalizationError(
            "result direction mismatch: expected HIB, "
            f"received {proportion or '<missing>'}"
        )

    system_results = result.get("results")
    if not isinstance(system_results, dict) or len(system_results) != 1:
        raise NormalizationError("expected exactly one candidate result")
    candidate = next(iter(system_results.values()))
    if not isinstance(candidate, dict):
        raise NormalizationError("candidate result must be an object")
    score = _finite_number(candidate.get("value"), "aggregate score")
    raw_values = candidate.get("raw_values")
    if not isinstance(raw_values, list) or not raw_values:
        raise NormalizationError("raw_values must contain at least one score")
    raw_scores = tuple(
        _finite_number(value, f"raw score {index}")
        for index, value in enumerate(raw_values)
    )
    if score <= 0 or any(value <= 0 for value in raw_scores):
        raise NormalizationError("PHPBench scores must be positive")
    return PhpBenchResult(score, raw_scores, profile, scale, proportion)


def _write_failed_result(output: Path, workload_id: str | None, message: str) -> int:
    emit_metric(
        output,
        "pts_run_ok",
        False,
        "flag",
        workload=workload_id,
        statistic="sample",
    )
    emit_metric(
        output,
        "profile_version_match",
        False,
        "flag",
        workload=workload_id,
        statistic="sample",
    )
    write_result(
        output,
        {
            "schemaVersion": "v1alpha1",
            "status": "failed",
            "checks": [
                {
                    "id": "pts-run-ok",
                    "passed": False,
                    "scope": "attempt",
                    "kind": "correctness",
                    "message": message,
                },
                {
                    "id": "profile-contract-match",
                    "passed": False,
                    "scope": "attempt",
                    "kind": "correctness",
                    "message": message,
                },
            ],
        },
    )
    with (output / "adapter.log").open("a", encoding="utf-8", newline="\n") as log:
        log.write(f"normalization=failed reason={message}\n")
    return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PTS PHPBench normalizer")
    parser.add_argument("--envelope", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)

    envelope = load_envelope(args.envelope)
    workload_id = envelope.get("workload", {}).get("id")
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    raw_path = output / "pts-result.json"
    try:
        raw = json.loads(raw_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise NormalizationError("PTS result root must be an object")
        normalized = parse_pts_result(raw)
    except (OSError, json.JSONDecodeError, NormalizationError) as error:
        return _write_failed_result(output, workload_id, str(error))

    emit_metric(
        output,
        "pts_run_ok",
        True,
        "flag",
        workload=workload_id,
        statistic="sample",
    )
    emit_metric(
        output,
        "profile_version_match",
        True,
        "flag",
        workload=workload_id,
        statistic="sample",
    )
    emit_metric(
        output,
        "phpbench_score",
        normalized.score,
        EXPECTED_SCALE,
        workload=workload_id,
        sample_count=len(normalized.raw_scores),
        statistic="mean",
        attributes={"profile": normalized.profile},
    )
    for index, score in enumerate(normalized.raw_scores):
        emit_metric(
            output,
            "phpbench_score_sample",
            score,
            EXPECTED_SCALE,
            workload=workload_id,
            sample_index=index,
            sample_count=len(normalized.raw_scores),
            statistic="sample",
            attributes={"profile": normalized.profile},
        )
    emit_metric(
        output,
        "sample_count",
        float(len(normalized.raw_scores)),
        "count",
        workload=workload_id,
        statistic="count",
    )
    write_result(
        output,
        {
            "schemaVersion": "v1alpha1",
            "status": "succeeded",
            "checks": [
                {
                    "id": "pts-run-ok",
                    "passed": True,
                    "scope": "attempt",
                    "kind": "correctness",
                    "message": "PTS exported one finite, positive PHPBench result",
                },
                {
                    "id": "profile-contract-match",
                    "passed": True,
                    "scope": "attempt",
                    "kind": "correctness",
                    "message": (
                        f"{normalized.profile} uses {normalized.scale}/{normalized.proportion}"
                    ),
                },
            ],
        },
    )
    with (output / "adapter.log").open("a", encoding="utf-8", newline="\n") as log:
        log.write(
            f"normalization=succeeded profile={normalized.profile} "
            f"samples={len(normalized.raw_scores)} score={normalized.score}\n"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

