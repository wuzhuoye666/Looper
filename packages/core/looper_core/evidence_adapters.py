"""Unified evidence adapters: one contract in, one Looper evidence manifest out.

Every adapter reads a benchmark's native output without modifying it, maps the
upstream fields onto the shared Looper evidence model, and records enough
provenance (raw digests, adapter identity, environment digest, benchmark
manifest digest) for the result to be independently re-verified.

Upstream-specific parsing lives exclusively in this layer. Downstream analysis
code only ever sees :class:`looper_core.evidence.EvidenceManifest`.
"""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

import yaml
from pydantic import Field

from looper_core.canonical import canonical_digest
from looper_core.contracts import (
    AttemptResult,
    MetricObservation,
    ResultArtifact,
    ResultCheck,
    StrictModel,
)
from looper_core.evidence import (
    AcceleratorSnapshot,
    AdapterIdentity,
    EnvironmentSnapshot,
    EvidenceArtifact,
    EvidenceError,
    EvidenceManifest,
    TraceFileEntry,
    TracePhaseRange,
    TraceSetManifest,
    digest_file,
    environment_digest,
    finalize_evidence,
)
from looper_core.scenario_adapters import (
    load_benchbase_smallbank_fixture,
    load_dcperf_mediawiki_fixture,
)


class EvidenceAdapterError(ValueError):
    pass


class RunContext(StrictModel):
    """Identity of the run whose native output is being normalized."""

    experiment_id: str | None = Field(default=None, alias="experimentId")
    candidate_id: str | None = Field(default=None, alias="candidateId")
    evaluation_id: str | None = Field(default=None, alias="evaluationId")
    attempt_id: str | None = Field(default=None, alias="attemptId")
    benchmark_id: str = Field(alias="benchmarkId", min_length=1, max_length=120)
    benchmark_version: str | None = Field(default=None, alias="benchmarkVersion", max_length=80)
    benchmark_manifest_digest: str | None = Field(default=None, alias="benchmarkManifestDigest")
    workload_id: str | None = Field(default=None, alias="workloadId", max_length=160)
    candidate_config_digest: str | None = Field(default=None, alias="candidateConfigDigest")
    environment: EnvironmentSnapshot


@dataclass(frozen=True)
class RawInput:
    """A native file an adapter intends to wrap as raw evidence.

    ``missing_policy`` controls what happens when a required file is absent:
    ``"raise"`` aborts normalization (the evidence cannot even be identified),
    while ``"record"`` keeps normalizing so the evidence manifest itself can
    record the failure -- this is how trace sets fail closed on missing ranks
    without discarding the whole attempt.
    """

    path: Path
    role: str
    media_type: str
    name: str
    required: bool = True
    producer: str = "upstream"
    provenance: str = "upstream-output"
    missing_policy: str = "raise"


@dataclass
class AdapterNormalization:
    """Adapter output before the framework wraps it into an evidence manifest."""

    document: dict[str, Any]
    observations: list[MetricObservation] = field(default_factory=list)
    checks: list[ResultCheck] = field(default_factory=list)
    status: str = "succeeded"
    message: str | None = None
    trace_sets: list[TraceSetManifest] = field(default_factory=list)
    workload_id: str | None = None
    normalized_name: str = "normalized-result.json"


@dataclass
class EvidenceBundle:
    """An evidence manifest plus the bytes of its normalized artifacts."""

    manifest: EvidenceManifest
    normalized_documents: dict[str, bytes]


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise EvidenceAdapterError(f"cannot read {path.name}: {error}") from error
    if not isinstance(value, dict):
        raise EvidenceAdapterError(f"{path.name} must contain a mapping")
    return value


def _metric_units_declared(
    observations: list[MetricObservation], declared: Mapping[str, str]
) -> None:
    for observation in observations:
        expected = declared.get(observation.metric)
        if expected is not None and observation.unit != expected:
            raise EvidenceAdapterError(
                f"metric {observation.metric} has unit {observation.unit!r} "
                f"but the adapter declares {expected!r}"
            )


class UnifiedAdapter(ABC):
    """Template for adapters that emit the unified evidence contract."""

    adapter_id: ClassVar[str]
    adapter_version: ClassVar[str]
    source_format: ClassVar[str]
    upstream_id: ClassVar[str | None] = None
    manifest_path: ClassVar[Path | None] = None
    synthetic: ClassVar[bool] = False
    compatibility_status: ClassVar[str] = "compatible"
    upstream_license: ClassVar[str | None] = None
    metric_units: ClassVar[Mapping[str, str]] = {}

    def __init__(self, *, synthetic: bool | None = None) -> None:
        self.synthetic = self.synthetic if synthetic is None else synthetic

    def implementation_digest(self) -> str:
        """Digest of the adapter manifest pinning this adapter's mapping."""

        if self.manifest_path is not None and self.manifest_path.is_file():
            digest, _size = digest_file(self.manifest_path)
            return digest
        return canonical_digest(
            {
                "adapterId": self.adapter_id,
                "adapterVersion": self.adapter_version,
                "sourceFormat": self.source_format,
            }
        )

    @abstractmethod
    def _raw_inputs(self, source: Path) -> list[RawInput]:
        """Declare the native files this adapter wraps (may parse the source)."""

    @abstractmethod
    def _normalize(self, source: Path, context: RunContext) -> AdapterNormalization:
        """Parse the native output and produce normalized evidence."""

    def normalize(self, source: Path, context: RunContext) -> EvidenceBundle:
        raw_inputs = self._raw_inputs(source)
        raw_artifacts: list[EvidenceArtifact] = []
        for raw in raw_inputs:
            if not raw.path.is_file():
                if raw.required and raw.missing_policy == "raise":
                    raise EvidenceAdapterError(f"required native input is missing: {raw.name}")
                continue
            digest, size = digest_file(raw.path)
            raw_artifacts.append(
                EvidenceArtifact(
                    digest=digest,
                    size=size,
                    role=raw.role,  # type: ignore[arg-type]
                    media_type=raw.media_type,
                    producer=raw.producer,
                    name=raw.name,
                    required=raw.required,
                    provenance=raw.provenance,  # type: ignore[arg-type]
                )
            )
        if not raw_artifacts:
            raise EvidenceAdapterError("no native input files were found")

        normalization = self._normalize(source, context)
        _metric_units_declared(normalization.observations, self.metric_units)

        normalized_bytes = (
            json.dumps(normalization.document, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        normalized_digest = f"sha256:{hashlib.sha256(normalized_bytes).hexdigest()}"
        normalized_artifacts = [
            EvidenceArtifact(
                digest=normalized_digest,
                size=len(normalized_bytes),
                role="result",
                media_type="application/json",
                producer=self.adapter_id,
                name=normalization.normalized_name,
                required=True,
                provenance="adapter-generated",
            )
        ]

        result = AttemptResult(
            schema_version="v1alpha1",  # type: ignore[arg-type]
            status=normalization.status,  # type: ignore[arg-type]
            message=normalization.message,
            checks=normalization.checks,
            artifacts=[
                ResultArtifact(
                    path=normalization.normalized_name,
                    role="result",
                    media_type="application/json",
                    description="normalized evidence document",
                )
            ],
            extensions={
                "adapterId": self.adapter_id,
                "adapterVersion": self.adapter_version,
                "synthetic": self.synthetic,
                "normalizationOnly": True,
            },
        )

        manifest = EvidenceManifest(
            schema_version="v1alpha1",  # type: ignore[arg-type]
            experiment_id=context.experiment_id,
            candidate_id=context.candidate_id,
            evaluation_id=context.evaluation_id,
            attempt_id=context.attempt_id,
            benchmark_id=context.benchmark_id,
            benchmark_version=context.benchmark_version,
            benchmark_manifest_digest=context.benchmark_manifest_digest,
            workload_id=normalization.workload_id or context.workload_id,
            candidate_config_digest=context.candidate_config_digest,
            environment_digest=environment_digest(context.environment),
            environment=context.environment,
            raw_artifacts=raw_artifacts,
            normalized_artifacts=normalized_artifacts,
            trace_sets=normalization.trace_sets,
            normalized_observations=normalization.observations,
            result=result,
            adapter=AdapterIdentity(
                adapter_id=self.adapter_id,
                adapter_version=self.adapter_version,
                implementation_digest=self.implementation_digest(),
                upstream_id=self.upstream_id,
                source_format=self.source_format,
                synthetic=self.synthetic,
                compatibility_status=self.compatibility_status,  # type: ignore[arg-type]
                upstream_license=self.upstream_license,  # type: ignore[arg-type]
            ),
        )
        finalized = finalize_evidence(manifest)
        return EvidenceBundle(
            manifest=finalized,
            normalized_documents={normalization.normalized_name: normalized_bytes},
        )


class CclWorkloadCardAdapter(UnifiedAdapter):
    """Original CCL-compatible adapter for synthetic workload cards.

    Reads the synthetic workload-card fixture, maps workload identity,
    parameters, parallelism, communication library, hardware requirements,
    declared metrics, and an optional trace set onto unified evidence. The
    upstream CCL-Bench license is unresolved, so this adapter is compatibility
    shaped only: no upstream code, schema, or data is copied.
    """

    adapter_id = "ccl-workload-card-compatible"
    adapter_version = "1.0.0"
    source_format = "workload-card-yaml"
    upstream_id = "ccl-bench"
    manifest_path = Path("adapters/ccl-workload-card/adapter.manifest.json")
    synthetic = True
    compatibility_status = "compatible"
    upstream_license = "unresolved"

    def _load_card(self, source: Path) -> dict[str, Any]:
        if source.is_dir():
            return _read_yaml(source / "workload-card.yaml")
        return _read_yaml(source)

    def _raw_inputs(self, source: Path) -> list[RawInput]:
        card = self._load_card(source)
        root = source if source.is_dir() else source.parent
        provenance = "synthetic-fixture" if self.synthetic else "upstream-output"
        inputs = [
            RawInput(
                path=root / "workload-card.yaml",
                role="workload-card",
                media_type="application/x-yaml",
                name="workload-card.yaml",
                producer="ccl-bench-compatible",
                provenance=provenance,
            )
        ]
        trace = card.get("trace")
        if isinstance(trace, dict):
            collector = trace.get("collector")
            producer = (
                str(collector.get("name", "trace-collector"))
                if isinstance(collector, dict)
                else "trace-collector"
            )
            for entry in trace.get("files", []):
                if not isinstance(entry, dict) or "path" not in entry:
                    raise EvidenceAdapterError("trace file entries require a path")
                path = root / str(entry["path"])
                inputs.append(
                    RawInput(
                        path=path,
                        role="trace",
                        media_type="application/json",
                        name=path.name,
                        producer=producer,
                        provenance="synthetic-fixture" if self.synthetic else "trace-collector",
                        # A missing rank fails closed inside the evidence
                        # (trace-set-completeness check) instead of aborting
                        # normalization, so incomplete captures stay auditable.
                        missing_policy="record",
                    )
                )
        return inputs

    def _normalize(self, source: Path, context: RunContext) -> AdapterNormalization:
        card = self._load_card(source)
        root = source if source.is_dir() else source.parent
        workload = card.get("workload")
        if not isinstance(workload, dict):
            raise EvidenceAdapterError("workload card is missing the workload section")
        workload_id = str(workload.get("id", "")).strip()
        if not workload_id:
            raise EvidenceAdapterError("workload card does not declare an id")

        catalog: dict[str, dict[str, Any]] = {}
        for entry in card.get("metrics", []):
            if not isinstance(entry, dict) or "name" not in entry or "unit" not in entry:
                raise EvidenceAdapterError("metric catalog entries require name and unit")
            catalog[str(entry["name"])] = entry

        observations: list[MetricObservation] = []
        unit_mismatches: list[dict[str, str]] = []
        results = card.get("results", [])
        if not isinstance(results, list):
            raise EvidenceAdapterError("results section must be a list")
        for entry in results:
            if not isinstance(entry, dict):
                raise EvidenceAdapterError("result entries must be objects")
            name = str(entry.get("metric", ""))
            declared = catalog.get(name)
            if declared is None:
                raise EvidenceAdapterError(f"result metric {name!r} is not in the metric catalog")
            unit = str(entry.get("unit", ""))
            if unit != str(declared["unit"]):
                unit_mismatches.append(
                    {"metric": name, "declared": str(declared["unit"]), "reported": unit}
                )
                continue
            value = entry.get("value")
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise EvidenceAdapterError(f"result metric {name!r} must be numeric")
            observations.append(
                MetricObservation(
                    schema_version="v1alpha1",  # type: ignore[arg-type]
                    metric=name,
                    value=float(value),
                    unit=unit,
                    phase="measurement",
                    workload=workload_id,
                    sample_count=entry.get("sample_count"),
                    statistic=str(entry.get("statistic", "sample")),  # type: ignore[arg-type]
                    attributes={
                        "adapter": self.adapter_id,
                        "synthetic": self.synthetic,
                        "direction": str(declared.get("direction", "none")),
                    },
                )
            )

        checks = [
            ResultCheck(
                id="workload-card-shape",
                passed=True,
                scope="attempt",
                kind="execution",
                message=None,
                details={"card_version": card.get("card_version")},
            ),
            ResultCheck(
                id="metric-unit-consistency",
                passed=not unit_mismatches,
                scope="attempt",
                kind="correctness",
                message=None if not unit_mismatches else "reported units differ from catalog",
                details={"mismatches": unit_mismatches},
            ),
        ]

        trace_sets: list[TraceSetManifest] = []
        trace = card.get("trace")
        if isinstance(trace, dict):
            trace_sets.append(self._build_trace_set(trace, root, workload_id, context, card))
        if trace_sets:
            incomplete = {
                trace_set.trace_set_id: trace_set.missing_ranks
                for trace_set in trace_sets
                if not trace_set.complete
            }
            checks.append(
                ResultCheck(
                    id="trace-set-completeness",
                    passed=not incomplete,
                    scope="attempt",
                    kind="execution",
                    message=None if not incomplete else "trace set is missing required ranks",
                    details={"missingRanks": incomplete},
                )
            )

        execution = card.get("execution", {})
        warmup_iterations = int(execution.get("warmup_iterations", 0))
        measured_iterations = int(execution.get("measured_iterations", 0))
        status = "succeeded" if all(check.passed for check in checks) else "failed"
        document = {
            "schema_version": "looper.normalized-workload-card/v1alpha1",
            "adapter": self.adapter_id,
            "upstream": self.upstream_id,
            "workload": {
                "id": workload_id,
                "title": workload.get("title"),
                "operation": workload.get("operation"),
                "data_type": workload.get("data_type"),
                "message_size_bytes": workload.get("message_size_bytes"),
                "participants": workload.get("participants"),
            },
            "execution": {
                "warmup_iterations": warmup_iterations,
                "measured_iterations": measured_iterations,
            },
            "parameters": {
                "data_type": workload.get("data_type"),
                "message_size": {"value": workload.get("message_size_bytes"), "unit": "byte"},
                "participant_count": {"value": workload.get("participants"), "unit": "count"},
            },
            "parallelism": card.get("parallelism"),
            "communication": card.get("communication"),
            "hardware": card.get("hardware"),
            "requirements": card.get("requirements"),
            "metrics": [
                {
                    "metric": observation.metric,
                    "value": observation.value,
                    "unit": observation.unit,
                    "statistic": observation.statistic,
                }
                for observation in observations
            ],
            "metric_catalog": card.get("metrics", []),
            "compatibility": {
                "synthetic": self.synthetic,
                "source_format": self.source_format,
                "compatibility_status": self.compatibility_status,
                "upstream_license": self.upstream_license,
            },
        }
        return AdapterNormalization(
            document=document,
            observations=observations,
            checks=checks,
            status=status,
            message=None if status == "succeeded" else "workload card consistency checks failed",
            trace_sets=trace_sets,
            workload_id=workload_id,
        )

    def _build_trace_set(
        self,
        trace: dict[str, Any],
        root: Path,
        workload_id: str,
        context: RunContext,
        card: dict[str, Any],
    ) -> TraceSetManifest:
        execution = card.get("execution", {})
        warmup_iterations = int(execution.get("warmup_iterations", 0))
        measured_iterations = int(execution.get("measured_iterations", 0))
        expected_ranks = int(trace.get("expected_ranks", 0))
        if expected_ranks < 1:
            raise EvidenceAdapterError("trace section requires expected_ranks >= 1")
        files: list[TraceFileEntry] = []
        present_ranks: set[int] = set()
        for entry in trace.get("files", []):
            if not isinstance(entry, dict):
                raise EvidenceAdapterError("trace file entries must be objects")
            rank = int(entry.get("rank", -1))
            path = root / str(entry["path"])
            if not path.is_file():
                continue
            digest, size = digest_file(path)
            files.append(
                TraceFileEntry(
                    rank=rank,
                    device=entry.get("device"),
                    artifact_digest=digest,
                    artifact_name=path.name,
                    size=size,
                )
            )
            present_ranks.add(rank)
        missing = sorted(set(range(expected_ranks)) - present_ranks)
        collector = trace.get("collector")
        collector_name = (
            str(collector.get("name", "unknown")) if isinstance(collector, dict) else "unknown"
        )
        collector_version = (
            str(collector.get("version", "0")) if isinstance(collector, dict) else "0"
        )
        collector_config_digest = (
            collector.get("config_digest") if isinstance(collector, dict) else None
        )
        return TraceSetManifest(
            schema_version="v1alpha1",  # type: ignore[arg-type]
            trace_set_id=f"{workload_id}-traces",
            attempt_id=context.attempt_id,
            benchmark_id=context.benchmark_id,
            workload_id=workload_id,
            format=str(trace.get("format", "looper-synthetic-json")),  # type: ignore[arg-type]
            time_unit=str(trace.get("time_unit", "microsecond")),  # type: ignore[arg-type]
            clock_domain=str(trace.get("clock_domain", "monotonic")),
            collector={
                "name": collector_name,
                "version": collector_version,
                "config_digest": collector_config_digest,
            },
            warmup=TracePhaseRange(start_step=0, end_step=warmup_iterations - 1)
            if warmup_iterations > 0
            else None,
            measurement=TracePhaseRange(
                start_step=warmup_iterations,
                end_step=warmup_iterations + measured_iterations - 1,
            ),
            step_boundary_rule=str(trace.get("step_boundary_rule", "unspecified")),
            expected_ranks=expected_ranks,
            files=files,
            complete=not missing,
            missing_ranks=missing,
            extensions={"synthetic": self.synthetic},
        )


class BenchbaseSmallbankEvidenceAdapter(UnifiedAdapter):
    """Wraps the existing BenchBase SmallBank scenario adapter as unified evidence."""

    adapter_id = "benchbase-smallbank-evidence"
    adapter_version = "1.0.0"
    source_format = "benchbase-smallbank-output"
    upstream_id = "benchbase"
    manifest_path = Path("adapters/benchbase-smallbank/adapter.manifest.json")
    synthetic = True
    metric_units = {
        "attempted_tps": "transactions/second",
        "committed_tps": "transactions/second",
        "committed_transactions": "transactions",
        "abort_ratio": "ratio",
        "retry_ratio": "ratio",
        "error_ratio": "ratio",
        "timeout_ratio": "ratio",
        "offered_load_achieved_ratio": "ratio",
        "rate_limiter_lag_ratio": "ratio",
        "client_headroom_ratio": "ratio",
        "latency_p50_ms": "ms",
        "latency_p95_ms": "ms",
        "latency_p99_ms": "ms",
        "latency_p999_ms": "ms",
        "latency_max_ms": "ms",
        "offered_tps": "transactions/second",
        "offered_requests": "transactions",
        "started_requests": "transactions",
        "completed_requests": "transactions",
        "timeout_count": "transactions",
    }

    def _raw_inputs(self, source: Path) -> list[RawInput]:
        provenance = "synthetic-fixture" if self.synthetic else "upstream-output"
        return [
            RawInput(
                path=source / "summary.json",
                role="result",
                media_type="application/json",
                name="summary.json",
                producer="benchbase",
                provenance=provenance,
            ),
            RawInput(
                path=source / "transaction-histograms.json",
                role="histogram",
                media_type="application/json",
                name="transaction-histograms.json",
                producer="benchbase",
                provenance=provenance,
            ),
            RawInput(
                path=source / "latency.raw.csv",
                role="histogram",
                media_type="text/csv",
                name="latency.raw.csv",
                producer="benchbase",
                provenance=provenance,
            ),
            RawInput(
                path=source / "client-load-accounting.json",
                role="result",
                media_type="application/json",
                name="client-load-accounting.json",
                producer="looper-client-load-accounting",
                provenance=provenance,
            ),
        ]

    def _normalize(self, source: Path, context: RunContext) -> AdapterNormalization:
        normalized = load_benchbase_smallbank_fixture(source)
        return _normalization_from_scenario_result(
            normalized,
            adapter_id=self.adapter_id,
            synthetic=self.synthetic,
            default_workload="smallbank",
        )


class DcperfMediawikiEvidenceAdapter(UnifiedAdapter):
    """Wraps the existing DCPerf MediaWiki scenario adapter as unified evidence."""

    adapter_id = "dcperf-mediawiki-evidence"
    adapter_version = "1.0.0"
    source_format = "dcperf-benchpress-result"
    upstream_id = "dcperf"
    manifest_path = Path("adapters/dcperf-mediawiki/adapter.manifest.json")
    synthetic = True
    metric_units = {
        "closed_loop_successful_rps": "requests/second",
        "wrk_rps": "requests/second",
        "successful_requests": "requests",
        "failed_request_ratio": "ratio",
        "error_ratio": "ratio",
        "timeout_count": "requests",
        "timeout_ratio": "ratio",
        "latency_p50_ms": "ms",
        "latency_p95_ms": "ms",
        "latency_p99_ms": "ms",
        "cpu_utilization_p95": "percent",
    }

    def _raw_inputs(self, source: Path) -> list[RawInput]:
        provenance = "synthetic-fixture" if self.synthetic else "upstream-output"
        return [
            RawInput(
                path=source,
                role="result",
                media_type="application/json",
                name="benchpress-result.json",
                producer="dcperf",
                provenance=provenance,
            )
        ]

    def _normalize(self, source: Path, context: RunContext) -> AdapterNormalization:
        normalized = load_dcperf_mediawiki_fixture(source)
        return _normalization_from_scenario_result(
            normalized,
            adapter_id=self.adapter_id,
            synthetic=self.synthetic,
            default_workload="oss_performance_mediawiki_mlp",
        )


def _normalization_from_scenario_result(
    normalized: dict[str, Any],
    *,
    adapter_id: str,
    synthetic: bool,
    default_workload: str,
) -> AdapterNormalization:
    """Convert a ``looper.scenario-result/v1alpha1`` document to the shared shape."""

    workload = str(normalized.get("workload") or default_workload)
    observations = [
        MetricObservation(
            schema_version="v1alpha1",  # type: ignore[arg-type]
            metric=str(metric["metric"]),
            value=float(metric["value"]),
            unit=str(metric["unit"]),
            phase="measurement",
            workload=workload,
            sample_count=metric.get("sample_count"),
            statistic=str(metric.get("statistic", "sample")),  # type: ignore[arg-type]
            attributes={
                "adapter": normalized.get("adapter"),
                "synthetic": synthetic,
            },
        )
        for metric in normalized.get("metrics", [])
    ]
    checks = [
        ResultCheck(
            id=str(check["id"]),
            passed=bool(check["passed"]),
            scope="attempt",
            kind=str(check.get("kind", "execution")),  # type: ignore[arg-type]
            message=None,
            details=dict(check.get("details", {})),
        )
        for check in normalized.get("checks", [])
    ]
    status = str(normalized.get("status", "failed"))
    return AdapterNormalization(
        document=normalized,
        observations=observations,
        checks=checks,
        status=status,  # type: ignore[arg-type]
        message=None if status == "succeeded" else "scenario adapter checks failed",
        trace_sets=[],
        workload_id=workload,
    )


def synthetic_gpu_environment() -> EnvironmentSnapshot:
    """A clearly-marked synthetic accelerator environment for fixtures and tests."""

    return EnvironmentSnapshot(
        schema_version="v1alpha1",
        environment_id="synthetic-gpu-env-001",
        synthetic=True,
        accelerators=[
            AcceleratorSnapshot(index=index, model="Synthetic GPU 16G") for index in range(8)
        ],
        accelerator_count=8,
        interconnect={"fabric": "synthetic-interconnect", "topology": None},
        communication_library={"name": "synthetic-ccl", "version": "1.0.0"},
        frameworks=[{"name": "synthetic-framework", "version": "1.0.0"}],
        performance_env_vars={"LOOPER_SYNTHETIC_EVIDENCE": "1"},
        extensions={"notice": "synthetic environment; not a real machine"},
    )


def environment_from_system_fingerprint(
    fingerprint: Mapping[str, Any],
    *,
    environment_id: str,
) -> EnvironmentSnapshot:
    """Layer the unified environment contract over a system fingerprint document.

    GPU-side fields stay null until a collector exists; they are never inferred
    from CPU-side data.
    """

    cpu = fingerprint.get("cpu") if isinstance(fingerprint.get("cpu"), dict) else {}
    memory = fingerprint.get("memory") if isinstance(fingerprint.get("memory"), dict) else {}
    return EnvironmentSnapshot(
        schema_version="v1alpha1",
        environment_id=environment_id,
        synthetic=False,
        system_fingerprint_digest=canonical_digest(dict(fingerprint)),
        hostname=fingerprint.get("hostname"),
        platform=fingerprint.get("platform"),
        cpu_model=cpu.get("model_name"),
        logical_cpu_count=fingerprint.get("logical_cpu_count"),
        numa_node_count=cpu.get("numa_node_count"),
        memory_total_bytes=memory.get("total_bytes"),
    )


def write_evidence_bundle(bundle: EvidenceBundle, output: Path) -> Path:
    """Materialize an evidence manifest and its normalized artifacts to disk."""

    output.mkdir(parents=True, exist_ok=True)
    manifest_path = output / "evidence-manifest.json"
    manifest_path.write_text(
        json.dumps(
            bundle.manifest.model_dump(mode="json", by_alias=True),
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    for name, content in bundle.normalized_documents.items():
        (output / name).write_bytes(content)
    return manifest_path


def load_evidence_manifest(path: Path) -> EvidenceManifest:
    """Load and validate an evidence manifest produced by any adapter."""

    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise EvidenceError(f"cannot read evidence manifest: {error}") from error
    return EvidenceManifest.model_validate(document)


def summarize_evidence(manifest: EvidenceManifest) -> dict[str, Any]:
    """Benchmark-agnostic view used by analysis, optimizers, and the Web/API.

    This function is the downstream read interface: it must never branch on
    benchmark identity or open upstream files.
    """

    return {
        "evidence_id": manifest.evidence_id,
        "benchmark": {
            "id": manifest.benchmark_id,
            "version": manifest.benchmark_version,
            "manifest_digest": manifest.benchmark_manifest_digest,
        },
        "workload": manifest.workload_id,
        "environment": manifest.environment_digest,
        "metrics": {
            observation.metric: observation.value
            for observation in manifest.normalized_observations
        },
        "raw_artifacts": [artifact.name for artifact in manifest.raw_artifacts],
        "normalized_artifacts": [artifact.name for artifact in manifest.normalized_artifacts],
        "trace_sets": [trace.trace_set_id for trace in manifest.trace_sets],
        "derived_metrics": [metric.metric for metric in manifest.derived_metrics],
        "result": manifest.result.status if manifest.result else None,
        "provenance": {
            "adapter": manifest.adapter.adapter_id,
            "adapter_version": manifest.adapter.adapter_version,
            "implementation_digest": manifest.adapter.implementation_digest,
            "synthetic": manifest.adapter.synthetic,
        },
    }
