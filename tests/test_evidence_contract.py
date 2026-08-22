"""Contract-level tests for the unified evidence model and its JSON schemas."""

from __future__ import annotations

from pathlib import Path

import pytest
from looper_core.canonical import canonical_digest
from looper_core.evidence import (
    DerivedMetric,
    EnvironmentSnapshot,
    EvidenceArtifact,
    EvidenceManifest,
    TraceFileEntry,
    TracePhaseRange,
    TraceSetManifest,
    environment_digest,
    evidence_content_digest,
    parameters_digest,
    validate_derived_metric_document,
    validate_environment_document,
    validate_evidence_document,
    validate_trace_set_document,
)
from looper_core.evidence_adapters import (
    CclWorkloadCardAdapter,
    RunContext,
    environment_from_system_fingerprint,
    synthetic_gpu_environment,
)
from looper_core.manifest import ManifestError
from looper_core.trace_evaluator import trace_evaluator_tool
from pydantic import ValidationError

DIGEST = "sha256:" + "a" * 64


def _ccl_bundle():
    context = RunContext(
        benchmarkId="ccl-bench-compatible",
        benchmarkVersion="1",
        workloadId="synthetic-allreduce-small",
        environment=synthetic_gpu_environment(),
    )
    return CclWorkloadCardAdapter().normalize(Path("adapters/ccl-workload-card/fixture"), context)


def _trace_set(complete: bool = True, missing: list[int] | None = None) -> dict:
    return {
        "schemaVersion": "v1alpha1",
        "traceSetId": "traces-1",
        "format": "looper-synthetic-json",
        "timeUnit": "microsecond",
        "clockDomain": "monotonic",
        "collector": {"name": "synthetic", "version": "1"},
        "warmup": {"startStep": 0, "endStep": 4},
        "measurement": {"startStep": 5, "endStep": 44},
        "stepBoundaryRule": "one step per iteration",
        "expectedRanks": 2,
        "files": [
            {
                "rank": 0,
                "device": 0,
                "artifactDigest": DIGEST,
                "artifactName": "trace-rank0.json",
                "size": 10,
            }
        ],
        "complete": complete,
        "missingRanks": missing if missing is not None else [1],
    }


def test_evidence_manifest_round_trips_through_schema() -> None:
    bundle = _ccl_bundle()
    document = bundle.manifest.model_dump(mode="json", by_alias=True)
    validate_evidence_document(document)
    reloaded = EvidenceManifest.model_validate(document)
    assert reloaded.evidence_id == bundle.manifest.evidence_id


def test_evidence_schema_rejects_unknown_fields() -> None:
    document = _ccl_bundle().manifest.model_dump(mode="json", by_alias=True)
    document["unexpectedField"] = True
    with pytest.raises(Exception, match="unexpectedField"):
        validate_evidence_document(document)
    with pytest.raises(ValidationError):
        EvidenceManifest.model_validate(document)


def test_evidence_schema_rejects_invalid_digests() -> None:
    document = _ccl_bundle().manifest.model_dump(mode="json", by_alias=True)
    document["environmentDigest"] = "md5:not-a-digest"
    with pytest.raises(Exception, match="environmentDigest"):
        validate_evidence_document(document)


def test_evidence_schema_rejects_wrong_types() -> None:
    document = _ccl_bundle().manifest.model_dump(mode="json", by_alias=True)
    document["rawArtifacts"] = "not-a-list"
    with pytest.raises(ManifestError):
        validate_evidence_document(document)


def test_environment_snapshot_schema_rejects_unknown_fields() -> None:
    document = synthetic_gpu_environment().model_dump(mode="json", by_alias=True)
    validate_environment_document(document)
    document["gpuGuess"] = "inferred"
    with pytest.raises(Exception, match="gpuGuess"):
        validate_environment_document(document)


def test_environment_snapshot_rejects_accelerator_count_mismatch() -> None:
    with pytest.raises(ValidationError, match="acceleratorCount"):
        EnvironmentSnapshot.model_validate(
            {
                "schemaVersion": "v1alpha1",
                "environmentId": "env-1",
                "acceleratorCount": 4,
                "accelerators": [
                    {"index": 0, "model": "Synthetic GPU"},
                    {"index": 1, "model": "Synthetic GPU"},
                ],
            }
        )


def test_environment_snapshot_does_not_infer_gpu_fields_from_cpu() -> None:
    fingerprint = {
        "schema_version": "looper.system-fingerprint/v1alpha1",
        "hostname": "host-1",
        "platform": "Windows",
        "logical_cpu_count": 8,
        "cpu": {"model_name": "Test CPU", "numa_node_count": 1},
        "memory": {"total_bytes": 16_000_000_000},
    }
    snapshot = environment_from_system_fingerprint(fingerprint, environment_id="env-fp")
    assert snapshot.synthetic is False
    assert snapshot.cpu_model == "Test CPU"
    assert snapshot.memory_total_bytes == 16_000_000_000
    assert snapshot.system_fingerprint_digest == canonical_digest(fingerprint)
    # GPU fields must stay null instead of being inferred.
    assert snapshot.accelerators == []
    assert snapshot.accelerator_count is None
    assert snapshot.cuda_version is None
    assert snapshot.communication_library is None
    validate_environment_document(snapshot.model_dump(mode="json", by_alias=True))


def test_trace_set_schema_rejects_unknown_fields_and_bad_digests() -> None:
    document = _trace_set()
    validate_trace_set_document(document)
    invalid = dict(document)
    invalid["clock"] = "monotonic"
    with pytest.raises(Exception, match="clock"):
        validate_trace_set_document(invalid)
    bad_digest = _trace_set()
    bad_digest["files"][0]["artifactDigest"] = "sha256:short"
    with pytest.raises(ManifestError):
        validate_trace_set_document(bad_digest)


def test_trace_set_model_enforces_missing_rank_consistency() -> None:
    document = _trace_set(complete=True, missing=[1])
    with pytest.raises(ValidationError, match="complete"):
        TraceSetManifest.model_validate(document)
    document = _trace_set(complete=False, missing=[])
    with pytest.raises(ValidationError, match="missingRanks"):
        TraceSetManifest.model_validate(document)


def test_trace_set_model_rejects_overlapping_phase_ranges() -> None:
    document = _trace_set(complete=False, missing=[1])
    document["warmup"] = {"startStep": 0, "endStep": 6}
    with pytest.raises(ValidationError, match="warmup"):
        TraceSetManifest.model_validate(document)


def test_trace_set_model_rejects_duplicate_ranks() -> None:
    document = _trace_set(complete=False, missing=[1, 2])
    document["expectedRanks"] = 3
    document["files"].append(dict(document["files"][0]))
    with pytest.raises(ValidationError, match="unique"):
        TraceSetManifest.model_validate(document)


def test_trace_phase_range_contains() -> None:
    phase = TracePhaseRange(start_step=5, end_step=44)
    assert phase.contains(5)
    assert phase.contains(44)
    assert not phase.contains(4)
    assert not phase.contains(45)


def test_trace_file_entry_requires_digest() -> None:
    with pytest.raises(ValidationError):
        TraceFileEntry.model_validate({"rank": 0, "artifactName": "t.json", "size": 1})


def test_derived_metric_schema_round_trip() -> None:
    tool = trace_evaluator_tool("1.0.0")
    metric = DerivedMetric(
        schema_version="v1alpha1",
        metric="average_step_time",
        value=1000.0,
        unit="microsecond",
        statistic="mean",
        analysis_tool_id=tool.tool_id,
        analysis_tool_version=tool.tool_version,
        analysis_tool_digest=tool.tool_digest,
        analysis_input_digest=DIGEST,
        input_artifact_digests=[DIGEST],
        trace_set_digest=DIGEST,
        parameters={"includeWarmup": False},
        parameters_digest=parameters_digest({"includeWarmup": False}),
        generated_at="2026-08-22T00:00:00Z",
    )
    document = metric.model_dump(mode="json", by_alias=True)
    validate_derived_metric_document(document)
    document["unexpected"] = 1
    with pytest.raises(Exception, match="unexpected"):
        validate_derived_metric_document(document)


def test_derived_metric_rejects_non_finite_values() -> None:
    tool = trace_evaluator_tool("1.0.0")
    with pytest.raises(ValidationError, match="finite"):
        DerivedMetric(
            schema_version="v1alpha1",
            metric="average_step_time",
            value=float("inf"),
            unit="microsecond",
            statistic="mean",
            analysis_tool_id=tool.tool_id,
            analysis_tool_version=tool.tool_version,
            analysis_tool_digest=tool.tool_digest,
            analysis_input_digest=DIGEST,
            parameters={},
            parameters_digest=parameters_digest({}),
            generated_at="2026-08-22T00:00:00Z",
        )


def test_evidence_manifest_rejects_duplicated_artifact_names() -> None:
    artifact = EvidenceArtifact(
        digest=DIGEST,
        size=1,
        role="result",
        media_type="application/json",
        producer="test",
        name="dup.json",
        provenance="upstream-output",
    )
    with pytest.raises(ValidationError, match="unique"):
        EvidenceManifest(
            schema_version="v1alpha1",
            benchmark_id="bench",
            environment_digest=environment_digest(synthetic_gpu_environment()),
            raw_artifacts=[artifact, artifact],
            adapter={
                "adapterId": "a",
                "adapterVersion": "1",
                "implementationDigest": DIGEST,
                "sourceFormat": "x",
            },
        )


def test_evidence_manifest_detects_environment_digest_mismatch() -> None:
    with pytest.raises(ValidationError, match="environmentDigest"):
        EvidenceManifest(
            schema_version="v1alpha1",
            benchmark_id="bench",
            environment_digest=DIGEST,
            environment=synthetic_gpu_environment(),
            adapter={
                "adapterId": "a",
                "adapterVersion": "1",
                "implementationDigest": DIGEST,
                "sourceFormat": "x",
            },
        )


def test_evidence_content_digest_is_stable_across_runs() -> None:
    first = _ccl_bundle().manifest
    second = _ccl_bundle().manifest
    assert evidence_content_digest(first) == evidence_content_digest(second)
    assert first.evidence_id == second.evidence_id


def test_evidence_content_digest_ignores_volatile_fields() -> None:
    manifest = _ccl_bundle().manifest
    payload = manifest.model_dump(mode="json", by_alias=True)
    payload["createdAt"] = "2030-01-01T00:00:00Z"
    payload["derivedMetrics"] = [
        {
            "schemaVersion": "v1alpha1",
            "metric": "average_step_time",
            "value": 1.0,
            "unit": "microsecond",
            "statistic": "mean",
            "analysisToolId": "t",
            "analysisToolVersion": "1",
            "analysisToolDigest": DIGEST,
            "analysisInputDigest": DIGEST,
            "parameters": {},
            "parametersDigest": parameters_digest({}),
            "generatedAt": "2030-01-01T00:00:00Z",
        }
    ]
    with_derived = EvidenceManifest.model_validate(payload)
    assert evidence_content_digest(with_derived) == evidence_content_digest(manifest)
