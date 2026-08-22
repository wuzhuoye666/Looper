"""Offline trace evaluation: recompute metrics without re-running the benchmark.

These tests walk the Stage 1 acceptance loop end to end: the synthetic CCL-style
output is normalized once, the raw traces are digested once, and every later
analysis change only re-reads the same immutable bytes.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from looper_core.evidence import TraceSetManifest
from looper_core.evidence_adapters import (
    CclWorkloadCardAdapter,
    RunContext,
    synthetic_gpu_environment,
)
from looper_core.trace_evaluator import (
    DerivedMetricLedger,
    TraceEvaluationError,
    TraceEvaluationParameters,
    analysis_input_digest,
    evaluate_trace_set,
    trace_evaluator_tool,
)

CCL_FIXTURE = Path("adapters/ccl-workload-card/fixture")


def _trace_set() -> TraceSetManifest:
    context = RunContext(
        benchmarkId="ccl-bench-compatible",
        environment=synthetic_gpu_environment(),
    )
    bundle = CclWorkloadCardAdapter().normalize(CCL_FIXTURE, context)
    return bundle.manifest.trace_sets[0]


def _payloads() -> dict[str, bytes]:
    return {
        "trace-rank0.json": (CCL_FIXTURE / "trace-rank0.json").read_bytes(),
        "trace-rank1.json": (CCL_FIXTURE / "trace-rank1.json").read_bytes(),
    }


def test_average_step_time_and_communication_ratio() -> None:
    metrics = evaluate_trace_set(
        _trace_set(),
        _payloads(),
        tool=trace_evaluator_tool("1.0.0"),
        parameters=TraceEvaluationParameters(),
        generated_at="2026-08-22T00:00:00Z",
    )
    by_metric = {metric.metric: metric for metric in metrics}
    step_time = by_metric["average_step_time"]
    assert step_time.value == pytest.approx(1000.0)
    assert step_time.unit == "microsecond"
    assert step_time.statistic == "mean"
    assert step_time.analysis_tool_id == "looper.trace-evaluator"
    ratio = by_metric["communication_time_ratio"]
    assert ratio.value == pytest.approx(0.15)
    assert ratio.unit == "ratio"


def test_same_trace_supports_two_tool_versions_side_by_side() -> None:
    trace = _trace_set()
    payloads = _payloads()
    ledger = DerivedMetricLedger()

    first = evaluate_trace_set(
        trace,
        payloads,
        tool=trace_evaluator_tool("1.0.0"),
        parameters=TraceEvaluationParameters(),
    )
    second = evaluate_trace_set(
        trace,
        payloads,
        tool=trace_evaluator_tool("2.0.0"),
        parameters=TraceEvaluationParameters(),
    )
    ledger.extend(first)
    ledger.extend(second)

    versions = ledger.tool_versions("average_step_time")
    assert versions == ["1.0.0", "2.0.0"]
    assert len(ledger.records()) == 4
    # Different tool identities produce different analysis input digests.
    assert first[0].analysis_input_digest != second[0].analysis_input_digest
    # But both point at the exact same immutable trace artifacts.
    assert first[0].trace_set_digest == second[0].trace_set_digest
    assert first[0].input_artifact_digests == second[0].input_artifact_digests


def test_reanalysis_with_new_parameters_never_reruns_the_benchmark() -> None:
    trace = _trace_set()
    payloads = _payloads()
    baseline_payload_digests = sorted(entry.artifact_digest for entry in trace.files)

    measurement_only = evaluate_trace_set(
        trace,
        payloads,
        tool=trace_evaluator_tool("1.0.0"),
        parameters=TraceEvaluationParameters(),
        generated_at="2026-08-22T00:00:00Z",
    )
    with_warmup = evaluate_trace_set(
        trace,
        payloads,
        tool=trace_evaluator_tool("1.0.0"),
        parameters=TraceEvaluationParameters(include_warmup=True),
        generated_at="2026-08-22T00:01:00Z",
    )

    baseline = {metric.metric: metric for metric in measurement_only}
    expanded = {metric.metric: metric for metric in with_warmup}
    # Including the five 1200us warmup steps changes both metrics.
    assert expanded["average_step_time"].value == pytest.approx(46000 / 45)
    assert expanded["average_step_time"].value > baseline["average_step_time"].value
    assert expanded["communication_time_ratio"].value == pytest.approx(7250 / 46000)
    # The analysis input digest records the parameter change...
    assert (
        baseline["average_step_time"].analysis_input_digest
        != expanded["average_step_time"].analysis_input_digest
    )
    # ...while the underlying raw trace digests stay byte-identical.
    for metric in measurement_only + with_warmup:
        assert sorted(metric.input_artifact_digests) == baseline_payload_digests
        assert metric.trace_set_digest == baseline["average_step_time"].trace_set_digest
    # The ledger keeps both policy results instead of overwriting.
    ledger = DerivedMetricLedger()
    ledger.extend(measurement_only)
    ledger.extend(with_warmup)
    assert len(ledger.for_trace_set(baseline["average_step_time"].trace_set_digest)) == 4


def test_ledger_is_append_only() -> None:
    trace = _trace_set()
    payloads = _payloads()
    metrics = evaluate_trace_set(
        trace,
        payloads,
        tool=trace_evaluator_tool("1.0.0"),
        parameters=TraceEvaluationParameters(),
    )
    ledger = DerivedMetricLedger()
    ledger.extend(metrics)
    with pytest.raises(TraceEvaluationError, match="append-only"):
        ledger.append(metrics[0])


def test_incomplete_trace_set_fails_closed() -> None:
    trace = _trace_set()
    document = trace.model_dump(mode="json", by_alias=True)
    # Simulate a lost rank while keeping the manifest internally consistent.
    document["files"] = [entry for entry in document["files"] if entry["rank"] == 0]
    document["missingRanks"] = [1]
    document["complete"] = False
    incomplete = TraceSetManifest.model_validate(document)
    with pytest.raises(TraceEvaluationError, match="incomplete"):
        evaluate_trace_set(
            incomplete,
            {"trace-rank0.json": _payloads()["trace-rank0.json"]},
            tool=trace_evaluator_tool("1.0.0"),
            parameters=TraceEvaluationParameters(),
        )


def test_missing_payload_from_cas_fails_closed() -> None:
    trace = _trace_set()
    with pytest.raises(TraceEvaluationError, match="was not provided"):
        evaluate_trace_set(
            trace,
            {},
            tool=trace_evaluator_tool("1.0.0"),
            parameters=TraceEvaluationParameters(),
        )


def test_analysis_input_digest_binds_everything() -> None:
    trace = _trace_set()
    base = analysis_input_digest(trace, trace_evaluator_tool("1.0.0"), TraceEvaluationParameters())
    other_version = analysis_input_digest(
        trace, trace_evaluator_tool("2.0.0"), TraceEvaluationParameters()
    )
    other_parameters = analysis_input_digest(
        trace, trace_evaluator_tool("1.0.0"), TraceEvaluationParameters(include_warmup=True)
    )
    assert len({base, other_version, other_parameters}) == 3
