"""Offline trace evaluation: derive metrics from captured traces without re-running.

The evaluator only consumes the :class:`~looper_core.evidence.TraceSetManifest`
and the raw trace artifacts it references. It never talks to a benchmark, so
changing the analysis policy or the tool version can never trigger a re-run.
Every result is a :class:`~looper_core.evidence.DerivedMetric` carrying the
tool identity, parameters, and input digests, and multiple tool versions can
coexist for the same trace.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from typing import Any

from pydantic import Field, field_validator

from looper_core.canonical import canonical_digest, utc_now_iso
from looper_core.contracts import StrictModel
from looper_core.evidence import (
    AnalysisToolIdentity,
    DerivedMetric,
    TraceSetManifest,
    parameters_digest,
    trace_set_digest,
)

TRACE_EVALUATOR_TOOL_ID = "looper.trace-evaluator"
TRACE_EVALUATOR_ALGORITHMS = ("average_step_time", "communication_time_ratio")


class TraceEvaluationError(ValueError):
    pass


def trace_evaluator_tool(version: str = "1.0.0") -> AnalysisToolIdentity:
    """Identity of this evaluator, digested over id/version/algorithms."""

    digest = canonical_digest(
        {
            "toolId": TRACE_EVALUATOR_TOOL_ID,
            "toolVersion": version,
            "algorithms": list(TRACE_EVALUATOR_ALGORITHMS),
        }
    )
    return AnalysisToolIdentity(
        tool_id=TRACE_EVALUATOR_TOOL_ID,
        tool_version=version,
        tool_digest=digest,
    )


class TraceEvaluationParameters(StrictModel):
    """Policy for one evaluation run over a trace set."""

    include_warmup: bool = Field(default=False, alias="includeWarmup")
    ranks: list[int] | None = None

    @field_validator("ranks")
    @classmethod
    def unique_ranks(cls, values: list[int] | None) -> list[int] | None:
        if values is not None:
            if len(values) != len(set(values)):
                raise ValueError("requested ranks must be unique")
            if any(rank < 0 for rank in values):
                raise ValueError("requested ranks must be non-negative")
        return values


class _SyntheticStep:
    __slots__ = ("index", "phase", "compute", "communication")

    def __init__(self, index: int, phase: str, compute: float, communication: float) -> None:
        self.index = index
        self.phase = phase
        self.compute = compute
        self.communication = communication

    @property
    def duration(self) -> float:
        return self.compute + self.communication


def _load_trace_document(payload: bytes | Mapping[str, Any], name: str) -> dict[str, Any]:
    if isinstance(payload, bytes):
        try:
            document = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise TraceEvaluationError(f"trace file {name} is not valid JSON: {error}") from error
    elif isinstance(payload, Mapping):
        document = dict(payload)
    else:
        raise TraceEvaluationError(f"trace file {name} has an unsupported payload type")
    if not isinstance(document, dict):
        raise TraceEvaluationError(f"trace file {name} must contain a JSON object")
    return document


def _parse_synthetic_steps(
    document: Mapping[str, Any], name: str
) -> tuple[int, list[_SyntheticStep]]:
    if document.get("schemaVersion") != "looper.synthetic-trace/v1alpha1":
        raise TraceEvaluationError(
            f"trace file {name} does not declare looper.synthetic-trace/v1alpha1"
        )
    rank = document.get("rank")
    if not isinstance(rank, int) or isinstance(rank, bool) or rank < 0:
        raise TraceEvaluationError(f"trace file {name} has an invalid rank")
    raw_steps = document.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise TraceEvaluationError(f"trace file {name} contains no steps")
    steps: list[_SyntheticStep] = []
    for position, entry in enumerate(raw_steps):
        if not isinstance(entry, dict):
            raise TraceEvaluationError(f"trace file {name} step {position} must be an object")
        index = entry.get("index")
        if not isinstance(index, int) or isinstance(index, bool):
            raise TraceEvaluationError(f"trace file {name} step {position} has an invalid index")
        if index != position:
            raise TraceEvaluationError(
                f"trace file {name} step indices must be contiguous starting at zero"
            )
        phase = entry.get("phase")
        if phase not in {"warmup", "measurement"}:
            raise TraceEvaluationError(
                f"trace file {name} step {position} has an unknown phase {phase!r}"
            )
        compute = entry.get("compute")
        communication = entry.get("communication")
        for value, label in ((compute, "compute"), (communication, "communication")):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TraceEvaluationError(
                    f"trace file {name} step {position} {label} duration must be numeric"
                )
            if not math.isfinite(float(value)) or float(value) < 0:
                raise TraceEvaluationError(
                    f"trace file {name} step {position} {label} duration must be finite and >= 0"
                )
        steps.append(_SyntheticStep(index, str(phase), float(compute), float(communication)))
    return rank, steps


def _selected_steps(
    steps: list[_SyntheticStep], trace: TraceSetManifest, include_warmup: bool
) -> list[_SyntheticStep]:
    selected = [
        step
        for step in steps
        if trace.measurement.contains(step.index)
        or (include_warmup and trace.warmup is not None and trace.warmup.contains(step.index))
    ]
    if not selected:
        raise TraceEvaluationError(
            "no steps fall inside the selected measurement window; the trace is incomplete"
        )
    return selected


def analysis_input_digest(
    trace: TraceSetManifest,
    tool: AnalysisToolIdentity,
    parameters: TraceEvaluationParameters,
) -> str:
    """Digest binding the trace set, artifacts, tool identity, and parameters."""

    return canonical_digest(
        {
            "traceSetDigest": trace_set_digest(trace),
            "artifactDigests": sorted(entry.artifact_digest for entry in trace.files),
            "toolId": tool.tool_id,
            "toolVersion": tool.tool_version,
            "toolDigest": tool.tool_digest,
            "parameters": parameters.model_dump(mode="json", by_alias=True),
        }
    )


def evaluate_trace_set(
    trace: TraceSetManifest,
    trace_payloads: Mapping[str, bytes | Mapping[str, Any]],
    *,
    tool: AnalysisToolIdentity,
    parameters: TraceEvaluationParameters,
    attempt_id: str | None = None,
    generated_at: str | None = None,
) -> list[DerivedMetric]:
    """Compute derived metrics for one trace set under one tool/parameter pair.

    ``trace_payloads`` maps artifact names from the trace set manifest to the
    raw trace content retrieved from the CAS.
    """

    if not trace.complete:
        raise TraceEvaluationError(
            f"trace set {trace.trace_set_id} is incomplete; missing ranks {trace.missing_ranks}"
        )
    if trace.format != "looper-synthetic-json":
        raise TraceEvaluationError(
            f"trace format {trace.format!r} is not implemented by this evaluator"
        )
    requested = parameters.ranks
    if requested is not None:
        available = {entry.rank for entry in trace.files}
        missing = sorted(set(requested) - available)
        if missing:
            raise TraceEvaluationError(f"requested ranks are missing from the trace set: {missing}")

    per_rank: dict[int, list[_SyntheticStep]] = {}
    for entry in trace.files:
        if requested is not None and entry.rank not in requested:
            continue
        payload = trace_payloads.get(entry.artifact_name)
        if payload is None:
            raise TraceEvaluationError(
                f"trace artifact {entry.artifact_name} was not provided by the CAS"
            )
        document = _load_trace_document(payload, entry.artifact_name)
        rank, steps = _parse_synthetic_steps(document, entry.artifact_name)
        if rank != entry.rank:
            raise TraceEvaluationError(
                f"trace file {entry.artifact_name} declares rank {rank} "
                f"but the manifest maps it to rank {entry.rank}"
            )
        per_rank[rank] = _selected_steps(steps, trace, parameters.include_warmup)
    if not per_rank:
        raise TraceEvaluationError("no trace ranks were selected for evaluation")

    step_time_per_rank = {
        rank: sum(step.duration for step in steps) / len(steps) for rank, steps in per_rank.items()
    }
    average_step_time = sum(step_time_per_rank.values()) / len(step_time_per_rank)
    total_compute = sum(step.compute for steps in per_rank.values() for step in steps)
    total_communication = sum(step.communication for steps in per_rank.values() for step in steps)
    total_duration = total_compute + total_communication
    if total_duration <= 0:
        raise TraceEvaluationError("trace durations sum to zero; cannot derive ratios")
    communication_ratio = total_communication / total_duration

    input_digest = analysis_input_digest(trace, tool, parameters)
    params = parameters.model_dump(mode="json", by_alias=True)
    params_digest = parameters_digest(params)
    timestamp = generated_at or utc_now_iso()
    artifact_digests = sorted(entry.artifact_digest for entry in trace.files)
    set_digest = trace_set_digest(trace)

    def derived(metric: str, value: float, unit: str, statistic: str) -> DerivedMetric:
        return DerivedMetric(
            schema_version="v1alpha1",
            metric=metric,
            value=value,
            unit=unit,
            statistic=statistic,  # type: ignore[arg-type]
            analysis_tool_id=tool.tool_id,
            analysis_tool_version=tool.tool_version,
            analysis_tool_digest=tool.tool_digest,
            analysis_input_digest=input_digest,
            input_artifact_digests=artifact_digests,
            trace_set_digest=set_digest,
            attempt_id=attempt_id,
            parameters=params,
            parameters_digest=params_digest,
            generated_at=timestamp,
        )

    return [
        derived("average_step_time", average_step_time, trace.time_unit, "mean"),
        derived("communication_time_ratio", communication_ratio, "ratio", "rate"),
    ]


class DerivedMetricLedger:
    """Append-only store: tool versions coexist, nothing is ever overwritten."""

    def __init__(self) -> None:
        self._records: list[DerivedMetric] = []

    @staticmethod
    def _key(metric: DerivedMetric) -> tuple[str, str, str, str, str | None]:
        return (
            metric.metric,
            metric.analysis_tool_id,
            metric.analysis_tool_version,
            metric.parameters_digest,
            metric.trace_set_digest,
        )

    def append(self, metric: DerivedMetric) -> None:
        key = self._key(metric)
        if any(self._key(existing) == key for existing in self._records):
            raise TraceEvaluationError(
                f"derived metric {metric.metric!r} already recorded for this tool, "
                "parameters, and trace set; the ledger is append-only"
            )
        self._records.append(metric)

    def extend(self, metrics: list[DerivedMetric]) -> None:
        for metric in metrics:
            self.append(metric)

    def records(self) -> tuple[DerivedMetric, ...]:
        return tuple(self._records)

    def for_trace_set(self, trace_digest: str) -> tuple[DerivedMetric, ...]:
        return tuple(metric for metric in self._records if metric.trace_set_digest == trace_digest)

    def tool_versions(self, metric: str) -> list[str]:
        return sorted(
            {record.analysis_tool_version for record in self._records if record.metric == metric}
        )
