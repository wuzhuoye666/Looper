"""Unified evidence contracts for Stage 1 of the Looper data foundation.

Every benchmark keeps its native raw output untouched. Adapters wrap those
outputs into one immutable, digest-addressed evidence model so downstream
analysis, optimizers, and the Web/API never parse upstream formats again.

Layering rules:
- Raw Evidence: upstream files, byte-immutable, content-addressed in the CAS.
- Normalized Evidence: adapter output, still immutable and digest-addressed.
- Derived Metrics: post-hoc computations over raw/normalized evidence. They
  never mutate the evidence they were computed from and can be regenerated
  with new tool versions without re-running a benchmark.
"""

from __future__ import annotations

import hashlib
import math
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from looper_core.canonical import canonical_digest, utc_now_iso
from looper_core.contracts import AttemptResult, MetricObservation, StrictModel
from looper_core.manifest import validate_document

DIGEST_PATTERN = r"^sha256:[0-9a-f]{64}$"

ArtifactRole = Literal[
    "log",
    "trace",
    "result",
    "profile",
    "dataset",
    "histogram",
    "workload-card",
    "environment-snapshot",
    "evidence-manifest",
    "trace-set-manifest",
    "derived-metric",
    "metrics",
    "other",
]

ProvenanceKind = Literal[
    "upstream-output",
    "synthetic-fixture",
    "adapter-generated",
    "trace-collector",
    "environment-collector",
    "analysis-tool",
]

TraceFormat = Literal["pytorch-kineto", "xprof", "nsys", "looper-synthetic-json"]

TimeUnit = Literal["nanosecond", "microsecond", "millisecond", "second"]

Statistic = Literal[
    "sample",
    "mean",
    "median",
    "p50",
    "p95",
    "p99",
    "p99.9",
    "maximum",
    "rate",
    "cvar99",
    "count",
    "boolean",
]


class EvidenceError(ValueError):
    pass


class EvidenceArtifact(StrictModel):
    """One content-addressed file inside an evidence manifest."""

    digest: str = Field(pattern=DIGEST_PATTERN)
    size: int = Field(ge=0)
    role: ArtifactRole
    media_type: str = Field(alias="mediaType", min_length=3, max_length=160)
    producer: str = Field(min_length=1, max_length=160)
    name: str = Field(min_length=1, max_length=255)
    required: bool = True
    provenance: ProvenanceKind


class AcceleratorSnapshot(StrictModel):
    """A single GPU/accelerator. Uncollectable fields stay null, never guessed."""

    index: int = Field(ge=0)
    model: str | None = None
    uuid: str | None = Field(default=None, max_length=120)
    memory_total_mib: int | None = Field(default=None, alias="memoryTotalMiB", ge=0)
    pcie_link: str | None = Field(default=None, alias="pcieLink", max_length=80)
    nvlink_peers: list[int] | None = Field(default=None, alias="nvlinkPeers")


class SoftwareComponent(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    version: str | None = Field(default=None, max_length=80)
    digest: str | None = Field(default=None, pattern=DIGEST_PATTERN)


class DiskSnapshot(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    kind: str | None = Field(default=None, max_length=80)
    size_gib: int | None = Field(default=None, alias="sizeGiB", ge=0)


class NicSnapshot(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    link_speed: str | None = Field(default=None, alias="linkSpeed", max_length=80)


class EnvironmentSnapshot(StrictModel):
    """Stable, versioned environment contract layered on the system fingerprint.

    CPU-side fields mirror ``looper.system-fingerprint/v1alpha1``. GPU,
    interconnect, driver, and communication-library fields are declared here so
    future accelerator benchmarks share one environment identity. Fields the
    current collector cannot observe stay ``null``; synthetic fixtures must set
    ``synthetic`` so they are never mistaken for a real machine.
    """

    schema_version: Literal["v1alpha1"] = Field(alias="schemaVersion")
    environment_id: str = Field(alias="environmentId", min_length=1, max_length=160)
    synthetic: bool = False
    system_fingerprint_digest: str | None = Field(
        default=None, alias="systemFingerprintDigest", pattern=DIGEST_PATTERN
    )
    hostname: str | None = Field(default=None, max_length=255)
    platform: str | None = Field(default=None, max_length=255)
    cpu_model: str | None = Field(default=None, alias="cpuModel", max_length=255)
    logical_cpu_count: int | None = Field(default=None, alias="logicalCpuCount", ge=0)
    numa_node_count: int | None = Field(default=None, alias="numaNodeCount", ge=0)
    memory_total_bytes: int | None = Field(default=None, alias="memoryTotalBytes", ge=0)
    disks: list[DiskSnapshot] = Field(default_factory=list)
    nics: list[NicSnapshot] = Field(default_factory=list)
    accelerators: list[AcceleratorSnapshot] = Field(default_factory=list)
    accelerator_count: int | None = Field(default=None, alias="acceleratorCount", ge=0)
    interconnect: dict[str, Any] | None = None
    driver_version: str | None = Field(default=None, alias="driverVersion", max_length=80)
    cuda_version: str | None = Field(default=None, alias="cudaVersion", max_length=80)
    rocm_version: str | None = Field(default=None, alias="rocmVersion", max_length=80)
    communication_library: SoftwareComponent | None = Field(
        default=None, alias="communicationLibrary"
    )
    frameworks: list[SoftwareComponent] = Field(default_factory=list)
    compiler: SoftwareComponent | None = None
    container_image_digest: str | None = Field(
        default=None, alias="containerImageDigest", pattern=DIGEST_PATTERN
    )
    performance_env_vars: dict[str, str] = Field(default_factory=dict, alias="performanceEnvVars")
    extensions: dict[str, Any] = Field(default_factory=dict)
    collected_at: str | None = Field(default=None, alias="collectedAt")

    @field_validator("performance_env_vars")
    @classmethod
    def reject_empty_keys(cls, values: dict[str, str]) -> dict[str, str]:
        for key in values:
            if not key:
                raise ValueError("performance environment variable names cannot be empty")
        return values

    @model_validator(mode="after")
    def validate_accelerator_count(self) -> EnvironmentSnapshot:
        if (
            self.accelerator_count is not None
            and self.accelerators
            and self.accelerator_count != len(self.accelerators)
        ):
            raise ValueError("acceleratorCount must match the accelerator list length")
        return self


def environment_digest(snapshot: EnvironmentSnapshot) -> str:
    return canonical_digest(snapshot.model_dump(mode="json", by_alias=True))


class TraceCollector(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    version: str = Field(min_length=1, max_length=80)
    config_digest: str | None = Field(default=None, alias="configDigest", pattern=DIGEST_PATTERN)


class TracePhaseRange(StrictModel):
    """Inclusive step-index range for one measurement phase."""

    start_step: int = Field(alias="startStep", ge=0)
    end_step: int = Field(alias="endStep", ge=0)

    @model_validator(mode="after")
    def validate_order(self) -> TracePhaseRange:
        if self.end_step < self.start_step:
            raise ValueError("phase endStep cannot precede startStep")
        return self

    def contains(self, step: int) -> bool:
        return self.start_step <= step <= self.end_step


class TraceFileEntry(StrictModel):
    rank: int = Field(ge=0)
    device: int | None = Field(default=None, ge=0)
    artifact_digest: str = Field(alias="artifactDigest", pattern=DIGEST_PATTERN)
    artifact_name: str = Field(alias="artifactName", min_length=1, max_length=255)
    size: int = Field(ge=0)


class TraceSetManifest(StrictModel):
    """Index over a set of per-rank/device trace files stored in the CAS.

    Raw trace payloads stay in the CAS; this manifest only records identity,
    timing semantics, rank mapping, and completeness so evaluators can fail
    closed on incomplete captures without opening the payloads.
    """

    schema_version: Literal["v1alpha1"] = Field(alias="schemaVersion")
    trace_set_id: str = Field(alias="traceSetId", min_length=1, max_length=160)
    attempt_id: str | None = Field(default=None, alias="attemptId")
    benchmark_id: str | None = Field(default=None, alias="benchmarkId")
    workload_id: str | None = Field(default=None, alias="workloadId")
    format: TraceFormat
    time_unit: TimeUnit = Field(alias="timeUnit")
    clock_domain: str = Field(alias="clockDomain", min_length=1, max_length=80)
    collector: TraceCollector
    warmup: TracePhaseRange | None = None
    measurement: TracePhaseRange
    step_boundary_rule: str = Field(alias="stepBoundaryRule", min_length=1, max_length=500)
    expected_ranks: int = Field(alias="expectedRanks", ge=1)
    files: list[TraceFileEntry] = Field(min_length=1)
    complete: bool
    missing_ranks: list[int] = Field(default_factory=list, alias="missingRanks")
    capture_overhead_ratio: float | None = Field(
        default=None, alias="captureOverheadRatio", ge=0, le=1
    )
    extensions: dict[str, Any] = Field(default_factory=dict)

    @field_validator("missing_ranks")
    @classmethod
    def unique_missing_ranks(cls, values: list[int]) -> list[int]:
        if len(values) != len(set(values)):
            raise ValueError("missing ranks must be unique")
        return values

    @model_validator(mode="after")
    def validate_ranks(self) -> TraceSetManifest:
        present = [entry.rank for entry in self.files]
        if len(present) != len(set(present)):
            raise ValueError("trace files must map to unique ranks")
        for rank in present:
            if rank >= self.expected_ranks:
                raise ValueError("trace file rank exceeds expectedRanks")
        for rank in self.missing_ranks:
            if rank >= self.expected_ranks:
                raise ValueError("missing rank exceeds expectedRanks")
        expected = set(range(self.expected_ranks))
        derived_missing = sorted(expected - set(present))
        if derived_missing != sorted(self.missing_ranks):
            raise ValueError("missingRanks must exactly match expected ranks without files")
        if self.complete != (not derived_missing):
            raise ValueError("complete must agree with the missing rank list")
        if self.warmup is not None and self.warmup.end_step >= self.measurement.start_step:
            raise ValueError("warmup range must end before the measurement range starts")
        return self


def trace_set_digest(trace_set: TraceSetManifest) -> str:
    return canonical_digest(trace_set.model_dump(mode="json", by_alias=True))


class AdapterIdentity(StrictModel):
    """Who produced the normalized evidence, at which implementation revision."""

    adapter_id: str = Field(alias="adapterId", min_length=1, max_length=120)
    adapter_version: str = Field(alias="adapterVersion", min_length=1, max_length=40)
    implementation_digest: str = Field(alias="implementationDigest", pattern=DIGEST_PATTERN)
    upstream_id: str | None = Field(default=None, alias="upstreamId", max_length=120)
    source_format: str = Field(alias="sourceFormat", min_length=1, max_length=120)
    synthetic: bool = False
    compatibility_status: Literal["native", "compatible", "reference-only"] = Field(
        default="compatible", alias="compatibilityStatus"
    )
    upstream_license: Literal["unresolved", "verified"] | None = Field(
        default=None, alias="upstreamLicense"
    )


class AnalysisToolIdentity(StrictModel):
    tool_id: str = Field(alias="toolId", min_length=1, max_length=120)
    tool_version: str = Field(alias="toolVersion", min_length=1, max_length=40)
    tool_digest: str = Field(alias="toolDigest", pattern=DIGEST_PATTERN)


class DerivedMetric(StrictModel):
    """A metric computed after the run from immutable evidence.

    Records the exact tool identity, parameters, and input digests so the same
    raw evidence can carry metrics from multiple tool versions side by side.
    Older results are never overwritten.
    """

    schema_version: Literal["v1alpha1"] = Field(alias="schemaVersion")
    metric: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]*$")
    value: float
    unit: str = Field(min_length=1, max_length=40)
    statistic: Statistic
    analysis_tool_id: str = Field(alias="analysisToolId", min_length=1, max_length=120)
    analysis_tool_version: str = Field(alias="analysisToolVersion", min_length=1, max_length=40)
    analysis_tool_digest: str = Field(alias="analysisToolDigest", pattern=DIGEST_PATTERN)
    analysis_input_digest: str = Field(alias="analysisInputDigest", pattern=DIGEST_PATTERN)
    input_artifact_digests: list[str] = Field(default_factory=list, alias="inputArtifactDigests")
    trace_set_digest: str | None = Field(
        default=None, alias="traceSetDigest", pattern=DIGEST_PATTERN
    )
    attempt_id: str | None = Field(default=None, alias="attemptId")
    parameters: dict[str, Any] = Field(default_factory=dict)
    parameters_digest: str = Field(alias="parametersDigest", pattern=DIGEST_PATTERN)
    generated_at: str = Field(alias="generatedAt", min_length=1, max_length=48)

    @field_validator("value")
    @classmethod
    def finite_value(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("derived metric values must be finite")
        return value

    @field_validator("input_artifact_digests")
    @classmethod
    def unique_inputs(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("input artifact digests must be unique")
        return values


def parameters_digest(parameters: dict[str, Any]) -> str:
    return canonical_digest(parameters)


class EvidenceManifest(StrictModel):
    """The complete, immutable evidence inventory for one attempt."""

    schema_version: Literal["v1alpha1"] = Field(alias="schemaVersion")
    evidence_id: str | None = Field(
        default=None, alias="evidenceId", pattern=r"^evidence_[0-9a-f]{16}$"
    )
    experiment_id: str | None = Field(default=None, alias="experimentId")
    candidate_id: str | None = Field(default=None, alias="candidateId")
    evaluation_id: str | None = Field(default=None, alias="evaluationId")
    attempt_id: str | None = Field(default=None, alias="attemptId")
    benchmark_id: str = Field(alias="benchmarkId", min_length=1, max_length=120)
    benchmark_version: str | None = Field(default=None, alias="benchmarkVersion", max_length=80)
    benchmark_manifest_digest: str | None = Field(
        default=None, alias="benchmarkManifestDigest", pattern=DIGEST_PATTERN
    )
    workload_id: str | None = Field(default=None, alias="workloadId", max_length=160)
    candidate_config_digest: str | None = Field(
        default=None, alias="candidateConfigDigest", pattern=DIGEST_PATTERN
    )
    environment_digest: str = Field(alias="environmentDigest", pattern=DIGEST_PATTERN)
    environment: EnvironmentSnapshot | None = None
    raw_artifacts: list[EvidenceArtifact] = Field(default_factory=list, alias="rawArtifacts")
    normalized_artifacts: list[EvidenceArtifact] = Field(
        default_factory=list, alias="normalizedArtifacts"
    )
    trace_sets: list[TraceSetManifest] = Field(default_factory=list, alias="traceSets")
    normalized_observations: list[MetricObservation] = Field(
        default_factory=list, alias="normalizedObservations"
    )
    derived_metrics: list[DerivedMetric] = Field(default_factory=list, alias="derivedMetrics")
    result: AttemptResult | None = None
    adapter: AdapterIdentity
    created_at: str | None = Field(default=None, alias="createdAt")

    @model_validator(mode="after")
    def validate_references(self) -> EvidenceManifest:
        artifact_names = [item.name for item in self.raw_artifacts]
        artifact_names += [item.name for item in self.normalized_artifacts]
        if len(artifact_names) != len(set(artifact_names)):
            raise ValueError("artifact names must be unique within an evidence manifest")
        trace_ids = [trace.trace_set_id for trace in self.trace_sets]
        if len(trace_ids) != len(set(trace_ids)):
            raise ValueError("trace set ids must be unique within an evidence manifest")
        raw_digests = {item.digest for item in self.raw_artifacts}
        for trace in self.trace_sets:
            if (
                trace.attempt_id is not None
                and self.attempt_id is not None
                and trace.attempt_id != self.attempt_id
            ):
                raise ValueError("trace set attempt id does not match the evidence attempt")
            for entry in trace.files:
                if entry.artifact_digest not in raw_digests:
                    raise ValueError(
                        f"trace file {entry.artifact_name} is not declared as a raw artifact"
                    )
        if self.environment is not None and self.environment_digest != environment_digest(
            self.environment
        ):
            raise ValueError("environmentDigest must match the embedded environment snapshot")
        return self


_VOLATILE_FIELDS = ("evidenceId", "createdAt", "derivedMetrics")


def evidence_content_digest(manifest: EvidenceManifest) -> str:
    """Digest of the immutable evidence content.

    Excludes the derived-metric appendix, the derived evidence id, and the
    creation timestamp so the same inputs always produce the same digest while
    later derived metrics never invalidate the underlying evidence identity.
    """

    payload = manifest.model_dump(mode="json", by_alias=True)
    for field in _VOLATILE_FIELDS:
        payload.pop(field, None)
    return canonical_digest(payload)


def finalize_evidence(manifest: EvidenceManifest) -> EvidenceManifest:
    """Assign the deterministic evidence id and default creation timestamp."""

    digest = evidence_content_digest(manifest)
    payload = manifest.model_dump(mode="json", by_alias=True)
    payload["evidenceId"] = f"evidence_{digest.removeprefix('sha256:')[:16]}"
    if not payload.get("createdAt"):
        payload["createdAt"] = utc_now_iso()
    return EvidenceManifest.model_validate(payload)


def digest_file(path: Path) -> tuple[str, int]:
    """Stream-hash a file without loading it fully into memory."""

    hasher = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            hasher.update(chunk)
            size += len(chunk)
    return f"sha256:{hasher.hexdigest()}", size


def validate_evidence_document(document: dict[str, Any]) -> None:
    validate_document(document, "evidence-manifest.schema.json")


def validate_environment_document(document: dict[str, Any]) -> None:
    validate_document(document, "environment-snapshot.schema.json")


def validate_trace_set_document(document: dict[str, Any]) -> None:
    validate_document(document, "trace-set.schema.json")


def validate_derived_metric_document(document: dict[str, Any]) -> None:
    validate_document(document, "derived-metric.schema.json")
