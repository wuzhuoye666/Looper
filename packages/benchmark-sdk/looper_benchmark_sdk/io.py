from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from looper_core.canonical import utc_now_iso
from looper_core.contracts import AttemptResult, MetricObservation


def load_envelope(path: str | Path) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("run envelope must be an object")
    return value


def emit_metric(
    output_dir: str | Path,
    metric: str,
    value: float | bool,
    unit: str,
    *,
    phase: str = "measurement",
    workload: str | None = None,
    sample_index: int | None = None,
    sample_count: int | None = None,
    statistic: str = "sample",
    attributes: dict[str, Any] | None = None,
) -> None:
    observation = MetricObservation(
        schemaVersion="v1alpha1",
        metric=metric,
        value=value,
        unit=unit,
        phase=phase,
        workload=workload,
        sampleIndex=sample_index,
        sampleCount=sample_count,
        statistic=statistic,
        timestamp=utc_now_iso(),
        attributes=attributes or {},
    )
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    with (output / "metrics.jsonl").open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(observation.model_dump_json(by_alias=True, exclude_none=True))
        stream.write("\n")


def write_result(output_dir: str | Path, result: AttemptResult | dict[str, Any]) -> Path:
    parsed = result if isinstance(result, AttemptResult) else AttemptResult.model_validate(result)
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    destination = output / "result.json"
    destination.write_text(
        parsed.model_dump_json(by_alias=True, exclude_none=True, indent=2), encoding="utf-8"
    )
    return destination
