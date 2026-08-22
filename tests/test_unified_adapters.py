"""Tests for the unified evidence adapters and the benchmark-agnostic read path."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest
import yaml
from looper_core.evidence import (
    evidence_content_digest,
    validate_evidence_document,
)
from looper_core.evidence_adapters import (
    BenchbaseSmallbankEvidenceAdapter,
    CclWorkloadCardAdapter,
    DcperfMediawikiEvidenceAdapter,
    EvidenceAdapterError,
    RunContext,
    load_evidence_manifest,
    summarize_evidence,
    synthetic_gpu_environment,
    write_evidence_bundle,
)
from pydantic import ValidationError

BENCHBASE_FIXTURE = Path("adapters/benchbase-smallbank/fixture")
DCPERF_FIXTURE = Path("adapters/dcperf-mediawiki/fixture/benchpress-result.json")
CCL_FIXTURE = Path("adapters/ccl-workload-card/fixture")


def _context(benchmark_id: str, **kwargs) -> RunContext:
    return RunContext(
        benchmarkId=benchmark_id,
        environment=synthetic_gpu_environment(),
        **kwargs,
    )


def test_three_native_formats_produce_one_evidence_model() -> None:
    bundles = {
        "ccl": CclWorkloadCardAdapter().normalize(CCL_FIXTURE, _context("ccl-bench-compatible")),
        "benchbase": BenchbaseSmallbankEvidenceAdapter().normalize(
            BENCHBASE_FIXTURE, _context("benchbase-smallbank")
        ),
        "dcperf": DcperfMediawikiEvidenceAdapter().normalize(
            DCPERF_FIXTURE, _context("dcperf-mediawiki")
        ),
    }
    for name, bundle in bundles.items():
        manifest = bundle.manifest
        assert manifest.schema_version == "v1alpha1"
        assert manifest.evidence_id is not None
        assert manifest.raw_artifacts, f"{name} must preserve raw artifacts"
        assert manifest.normalized_artifacts, f"{name} must declare normalized artifacts"
        assert manifest.normalized_observations, f"{name} must produce observations"
        assert manifest.result is not None and manifest.result.status == "succeeded"
        assert manifest.adapter.adapter_id
        assert manifest.adapter.implementation_digest.startswith("sha256:")
        validate_evidence_document(manifest.model_dump(mode="json", by_alias=True))

    # Each adapter keeps its own upstream identity instead of flattening it.
    assert bundles["ccl"].manifest.adapter.upstream_id == "ccl-bench"
    assert bundles["ccl"].manifest.workload_id == "synthetic-allreduce-small"
    assert bundles["benchbase"].manifest.workload_id == "smallbank"
    assert bundles["dcperf"].manifest.workload_id == "oss_performance_mediawiki_mlp"
    metrics = {
        observation.metric for observation in bundles["benchbase"].manifest.normalized_observations
    }
    assert {"committed_tps", "latency_p99_ms", "offered_tps"} <= metrics


def test_raw_artifact_bytes_and_digests_are_unchanged(tmp_path: Path) -> None:
    source = tmp_path / "fixture"
    shutil.copytree(BENCHBASE_FIXTURE, source)
    expected = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(source.iterdir())
    }
    bundle = BenchbaseSmallbankEvidenceAdapter().normalize(source, _context("benchbase-smallbank"))
    recorded = {artifact.name: artifact.digest for artifact in bundle.manifest.raw_artifacts}
    for name, digest in expected.items():
        assert recorded[name] == f"sha256:{digest}"
        assert hashlib.sha256((source / name).read_bytes()).hexdigest() == digest


def test_missing_required_artifact_fails_closed(tmp_path: Path) -> None:
    (tmp_path / "summary.json").write_text("{}", encoding="utf-8")
    with pytest.raises(EvidenceAdapterError, match="required native input is missing"):
        BenchbaseSmallbankEvidenceAdapter().normalize(tmp_path, _context("benchbase-smallbank"))


def test_metric_unit_mismatch_fails_closed(tmp_path: Path) -> None:
    source = tmp_path / "card"
    shutil.copytree(CCL_FIXTURE, source)
    card_path = source / "workload-card.yaml"
    card = yaml.safe_load(card_path.read_text(encoding="utf-8"))
    card["results"][0]["unit"] = "microseconds"  # catalog declares microsecond
    card_path.write_text(yaml.safe_dump(card), encoding="utf-8")

    bundle = CclWorkloadCardAdapter().normalize(source, _context("ccl-bench-compatible"))
    assert bundle.manifest.result is not None
    assert bundle.manifest.result.status == "failed"
    checks = {check.id: check for check in bundle.manifest.result.checks}
    assert checks["metric-unit-consistency"].passed is False
    assert checks["metric-unit-consistency"].details["mismatches"][0]["metric"] == "completion_time"
    # The inconsistent metric must not enter the normalized observations.
    assert "completion_time" not in {
        observation.metric for observation in bundle.manifest.normalized_observations
    }


def test_incomplete_trace_set_marks_evidence_failed(tmp_path: Path) -> None:
    source = tmp_path / "card"
    shutil.copytree(CCL_FIXTURE, source)
    (source / "trace-rank1.json").unlink()
    bundle = CclWorkloadCardAdapter().normalize(source, _context("ccl-bench-compatible"))
    manifest = bundle.manifest
    assert manifest.result is not None and manifest.result.status == "failed"
    trace = manifest.trace_sets[0]
    assert trace.complete is False
    assert trace.missing_ranks == [1]
    checks = {check.id: check for check in manifest.result.checks}
    assert checks["trace-set-completeness"].passed is False


def test_adapter_provenance_is_complete() -> None:
    context = _context(
        "ccl-bench-compatible",
        experimentId="exp-1",
        candidateId="cand-1",
        evaluationId="eval-1",
        attemptId="att-1",
        benchmarkVersion="1.2.0",
        benchmarkManifestDigest="sha256:" + "b" * 64,
        candidateConfigDigest="sha256:" + "c" * 64,
    )
    bundle = CclWorkloadCardAdapter().normalize(CCL_FIXTURE, context)
    manifest = bundle.manifest
    adapter = manifest.adapter
    assert adapter.adapter_id == "ccl-workload-card-compatible"
    assert adapter.adapter_version == "1.0.0"
    assert adapter.implementation_digest.startswith("sha256:")
    assert adapter.upstream_id == "ccl-bench"
    assert adapter.source_format == "workload-card-yaml"
    assert adapter.synthetic is True
    assert adapter.compatibility_status == "compatible"
    assert adapter.upstream_license == "unresolved"
    assert manifest.experiment_id == "exp-1"
    assert manifest.candidate_id == "cand-1"
    assert manifest.evaluation_id == "eval-1"
    assert manifest.attempt_id == "att-1"
    assert manifest.benchmark_version == "1.2.0"
    assert manifest.benchmark_manifest_digest == "sha256:" + "b" * 64
    assert manifest.candidate_config_digest == "sha256:" + "c" * 64
    assert manifest.environment is not None
    assert manifest.environment.synthetic is True

    # The implementation digest must actually pin the adapter manifest file.
    manifest_bytes = Path("adapters/ccl-workload-card/adapter.manifest.json").read_bytes()
    assert adapter.implementation_digest == f"sha256:{hashlib.sha256(manifest_bytes).hexdigest()}"

    # Every raw artifact carries full provenance.
    for artifact in manifest.raw_artifacts:
        assert artifact.role in {"workload-card", "trace"}
        assert artifact.provenance == "synthetic-fixture"
        assert artifact.size > 0
        assert artifact.required is True


def test_same_input_produces_stable_canonical_digest() -> None:
    context = _context(
        "benchbase-smallbank",
        benchmarkManifestDigest="sha256:" + "d" * 64,
    )
    first = BenchbaseSmallbankEvidenceAdapter().normalize(BENCHBASE_FIXTURE, context)
    second = BenchbaseSmallbankEvidenceAdapter().normalize(BENCHBASE_FIXTURE, context)
    assert first.manifest.evidence_id == second.manifest.evidence_id
    assert evidence_content_digest(first.manifest) == evidence_content_digest(second.manifest)
    assert first.normalized_documents == second.normalized_documents


def test_downstream_reader_never_branches_on_benchmark() -> None:
    manifests = [
        CclWorkloadCardAdapter().normalize(CCL_FIXTURE, _context("ccl-bench-compatible")).manifest,
        BenchbaseSmallbankEvidenceAdapter()
        .normalize(BENCHBASE_FIXTURE, _context("benchbase-smallbank"))
        .manifest,
        DcperfMediawikiEvidenceAdapter()
        .normalize(DCPERF_FIXTURE, _context("dcperf-mediawiki"))
        .manifest,
    ]
    summaries = [summarize_evidence(manifest) for manifest in manifests]
    expected_keys = {
        "evidence_id",
        "benchmark",
        "workload",
        "environment",
        "metrics",
        "raw_artifacts",
        "normalized_artifacts",
        "trace_sets",
        "derived_metrics",
        "result",
        "provenance",
    }
    for summary in summaries:
        assert expected_keys <= set(summary)
        assert summary["result"] == "succeeded"
        assert summary["metrics"]
    assert summaries[0]["trace_sets"] == ["synthetic-allreduce-small-traces"]
    assert summaries[1]["trace_sets"] == []
    assert summaries[2]["trace_sets"] == []


def test_evidence_bundle_round_trips_through_disk(tmp_path: Path) -> None:
    bundle = CclWorkloadCardAdapter().normalize(CCL_FIXTURE, _context("ccl-bench-compatible"))
    manifest_path = write_evidence_bundle(bundle, tmp_path / "bundle")
    reloaded = load_evidence_manifest(manifest_path)
    assert reloaded.evidence_id == bundle.manifest.evidence_id
    assert reloaded == bundle.manifest
    normalized_path = tmp_path / "bundle" / "normalized-result.json"
    document = json.loads(normalized_path.read_text(encoding="utf-8"))
    assert document["workload"]["id"] == "synthetic-allreduce-small"
    assert document["compatibility"]["upstream_license"] == "unresolved"


def test_run_context_requires_environment() -> None:
    with pytest.raises(ValidationError, match="environment"):
        RunContext.model_validate({"benchmarkId": "bench"})
