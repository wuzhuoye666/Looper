from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from looper_core.canonical import canonical_digest, canonical_json
from looper_core.cas import FileSystemCAS, StoredArtifact
from looper_core.contracts import Direction, ExperimentMode, ExperimentSpec, StrictModel
from looper_core.evidence import (
    AdapterIdentity,
    EvidenceArtifact,
    EvidenceManifest,
    finalize_evidence,
)
from looper_core.state import ExperimentStatus
from looper_core.system_opt.hypothesis import (
    CAPACITY_IDENTITY_FIELDS,
    hypothesis_context_digest,
)
from pydantic import Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from looper_api.models import (
    BenchmarkRecord,
    CapacityStudyRecord,
    EvaluationRecord,
    ExperimentRecord,
    SourceDiscoveryRecord,
)

CAPACITY_EVIDENCE_SCHEMA = "looper.capacity-study-evidence/v1alpha1"
CAPACITY_EVIDENCE_ADAPTER_ID = "looper.capacity-study"
CAPACITY_EVIDENCE_ADAPTER_VERSION = "1.0.0"
CAPACITY_EVIDENCE_IMPLEMENTATION_DIGEST = canonical_digest(
    {
        "schema": CAPACITY_EVIDENCE_SCHEMA,
        "workload": "business-iteration",
        "metric": "committed_tps",
        "identityFields": list(CAPACITY_IDENTITY_FIELDS),
        "frontier": ["status", "confirmed_pass", "confirmed_fail"],
    }
)
CAPACITY_REPORT_UNIT = "successful business iterations/second"
CAPACITY_METRIC_UNIT = "iterations/second"
CAPACITY_WORKLOAD_ID = "business-iteration"
CAPACITY_METRIC_ID = "committed_tps"


class CapacityEvidenceIssue(StrictModel):
    stage: Literal["capacity-evidence"] = "capacity-evidence"
    code: str = Field(min_length=1, max_length=120, pattern=r"^[a-z][a-z0-9_]*$")
    message: str = Field(min_length=1, max_length=2000)
    recoverable: bool
    details: dict[str, Any] = Field(default_factory=dict)


class CapacityEvidenceError(RuntimeError):
    def __init__(self, issue: CapacityEvidenceIssue) -> None:
        super().__init__(issue.message)
        self.issue = issue


class ResolvedCapacityFrontier(StrictModel):
    status: Literal["resolved"]
    confirmed_pass: float = Field(gt=0)
    confirmed_fail: float = Field(gt=0)

    @model_validator(mode="after")
    def validate_interval(self) -> ResolvedCapacityFrontier:
        if self.confirmed_pass > self.confirmed_fail:
            raise ValueError("confirmed_pass cannot exceed confirmed_fail")
        return self


class CapacityStudyEvidence(StrictModel):
    schema_version: Literal[CAPACITY_EVIDENCE_SCHEMA]
    study_id: str = Field(min_length=1, max_length=80)
    experiment_id: str = Field(min_length=1, max_length=80)
    target_id: str = Field(min_length=1, max_length=100)
    network: Literal["internal", "external"]
    workload_id: Literal[CAPACITY_WORKLOAD_ID]
    metric_id: Literal[CAPACITY_METRIC_ID]
    report_digest: str
    study_contract_digest: str
    experiment_contract_digest: str
    benchmark_manifest_digest: str
    frontier: ResolvedCapacityFrontier
    control_frontiers: dict[str, ResolvedCapacityFrontier]
    active_target_ids: list[str] = Field(min_length=1)
    identity: dict[str, str]
    context_digest: str

    @model_validator(mode="after")
    def validate_identity(self) -> CapacityStudyEvidence:
        if self.target_id not in self.active_target_ids:
            raise ValueError("target_id must belong to active_target_ids")
        if len(self.active_target_ids) != len(set(self.active_target_ids)):
            raise ValueError("active_target_ids must be unique")
        expected_controls = set(self.active_target_ids) - {self.target_id}
        if set(self.control_frontiers) != expected_controls:
            raise ValueError("control_frontiers must cover every untuned active target")
        if self.context_digest != hypothesis_context_digest(self.identity):
            raise ValueError("context_digest does not match the capacity identity")
        return self


@dataclass(frozen=True, slots=True)
class CapacityEvidenceBundle:
    evidence: CapacityStudyEvidence
    manifest: EvidenceManifest
    report_artifact: StoredArtifact
    study_contract_artifact: StoredArtifact
    experiment_contract_artifact: StoredArtifact
    normalized_artifact: StoredArtifact
    manifest_artifact: StoredArtifact


def _issue(
    code: str,
    message: str,
    *,
    recoverable: bool = False,
    **details: Any,
) -> CapacityEvidenceError:
    return CapacityEvidenceError(
        CapacityEvidenceIssue(
            code=code,
            message=message,
            recoverable=recoverable,
            details=details,
        )
    )


def _one(items: list[Any], *, code: str, message: str, **details: Any) -> Any:
    if len(items) != 1:
        raise _issue(code, message, count=len(items), **details)
    return items[0]


def _json_bytes(value: Any) -> bytes:
    return canonical_json(value).encode("utf-8")


def _put_json(cas: FileSystemCAS, value: Any) -> StoredArtifact:
    return cas.put_bytes(_json_bytes(value))


def _artifact(
    stored: StoredArtifact,
    *,
    name: str,
    provenance: Literal["upstream-output", "adapter-generated"],
) -> EvidenceArtifact:
    return EvidenceArtifact(
        digest=stored.digest,
        size=stored.size,
        role="result",
        mediaType="application/json",
        producer=CAPACITY_EVIDENCE_ADAPTER_ID,
        name=name,
        provenance=provenance,
    )


def _frontier(target: dict[str, Any], *, target_id: str) -> ResolvedCapacityFrontier:
    frontiers = target.get("frontiers")
    if not isinstance(frontiers, dict):
        raise _issue(
            "capacity_report_contract_changed",
            "capacity target does not contain a frontiers object",
            target_id=target_id,
        )
    raw = frontiers.get(CAPACITY_WORKLOAD_ID)
    if not isinstance(raw, dict):
        raise _issue(
            "capacity_frontier_missing",
            "business-iteration capacity frontier is missing",
            target_id=target_id,
            available_workloads=sorted(str(key) for key in frontiers),
        )
    try:
        # Reports retain adaptive-search audit fields alongside the stable
        # evidence contract. Validate only the fields consumed by the
        # optimizer, while preserving the full report artifact separately.
        return ResolvedCapacityFrontier.model_validate(
            {
                "status": raw.get("status"),
                "confirmed_pass": raw.get("confirmed_pass"),
                "confirmed_fail": raw.get("confirmed_fail"),
            }
        )
    except ValueError as error:
        raise _issue(
            "capacity_frontier_unresolved",
            "capacity frontier is not a closed positive interval",
            recoverable=True,
            target_id=target_id,
            frontier=raw,
        ) from error


def _report_target(report_network: dict[str, Any], target_id: str) -> dict[str, Any]:
    targets = report_network.get("targets")
    if not isinstance(targets, list):
        raise _issue(
            "capacity_report_contract_changed",
            "capacity network does not contain a targets list",
            target_id=target_id,
        )
    matches = [
        item
        for item in targets
        if isinstance(item, dict) and item.get("target_id") == target_id
    ]
    return _one(
        matches,
        code="capacity_target_ambiguous",
        message="capacity report must contain the target exactly once",
        target_id=target_id,
    )


def _benchmark(
    session: Session, spec: ExperimentSpec, experiment_id: str
) -> BenchmarkRecord:
    matches = list(
        session.scalars(
            select(BenchmarkRecord).where(
                BenchmarkRecord.benchmark_id == spec.benchmark_id,
                BenchmarkRecord.version == spec.benchmark_version,
            )
        )
    )
    return _one(
        matches,
        code="capacity_benchmark_missing",
        message="capacity experiment benchmark identity is unavailable or ambiguous",
        experiment_id=experiment_id,
        benchmark_id=spec.benchmark_id,
        benchmark_version=spec.benchmark_version,
    )


def _evaluation(
    session: Session, experiment_id: str, target_id: str
) -> EvaluationRecord:
    matches = list(
        session.scalars(
            select(EvaluationRecord).where(
                EvaluationRecord.experiment_id == experiment_id,
                EvaluationRecord.target_id == target_id,
                EvaluationRecord.workload_id == CAPACITY_WORKLOAD_ID,
            )
        )
    )
    return _one(
        matches,
        code="capacity_environment_missing",
        message="capacity target environment snapshot is unavailable or ambiguous",
        experiment_id=experiment_id,
        target_id=target_id,
    )


def build_capacity_study_evidence(
    session: Session,
    record: CapacityStudyRecord,
    cas: FileSystemCAS,
    *,
    target_id: str,
    network: Literal["internal", "external"],
) -> CapacityEvidenceBundle:
    """Normalize one completed capacity-study target without mutating database state."""

    if record.status != "completed" or record.report_json is None or record.completed_at is None:
        raise _issue(
            "baseline_incomplete",
            "capacity study must be completed and have a persisted report",
            recoverable=True,
            study_id=record.id,
            status=record.status,
        )
    discovery = session.get(SourceDiscoveryRecord, record.discovery_id)
    if discovery is None:
        raise _issue(
            "source_identity_missing",
            "capacity study source discovery no longer exists",
            study_id=record.id,
            discovery_id=record.discovery_id,
        )

    active_target_ids = record.execution_json.get("activeTargetIds")
    if not isinstance(active_target_ids, list) or not active_target_ids:
        raise _issue(
            "capacity_execution_contract_changed",
            "capacity execution does not declare activeTargetIds",
            study_id=record.id,
        )
    active_target_ids = [str(item) for item in active_target_ids]
    if len(active_target_ids) != len(set(active_target_ids)) or target_id not in active_target_ids:
        raise _issue(
            "capacity_target_not_active",
            "selected target is absent from the immutable capacity target set",
            study_id=record.id,
            target_id=target_id,
            active_target_ids=active_target_ids,
        )

    runs = record.execution_json.get("runs")
    if not isinstance(runs, list):
        raise _issue(
            "capacity_execution_contract_changed",
            "capacity execution does not contain a runs list",
            study_id=record.id,
        )
    run = _one(
        [item for item in runs if isinstance(item, dict) and item.get("network") == network],
        code="capacity_network_ambiguous",
        message="capacity execution must contain the selected network exactly once",
        study_id=record.id,
        network=network,
    )
    experiment_id = str(run.get("experimentId") or "")
    if not experiment_id:
        raise _issue(
            "capacity_experiment_missing",
            "capacity run does not declare an experimentId",
            study_id=record.id,
            network=network,
        )

    experiment = session.get(ExperimentRecord, experiment_id)
    if experiment is None or ExperimentStatus(experiment.status) != ExperimentStatus.COMPLETED:
        raise _issue(
            "capacity_experiment_incomplete",
            "capacity experiment must exist and be completed",
            recoverable=True,
            experiment_id=experiment_id,
            status=experiment.status if experiment is not None else None,
        )
    try:
        spec = ExperimentSpec.model_validate(experiment.spec_json)
    except ValueError as error:
        raise _issue(
            "capacity_experiment_contract_changed",
            "capacity experiment spec no longer validates",
            experiment_id=experiment_id,
        ) from error
    if (
        spec.mode != ExperimentMode.SELECTION
        or spec.scenario is None
        or spec.selection is None
        or spec.scenario.primary_metric != CAPACITY_METRIC_ID
        or CAPACITY_WORKLOAD_ID not in spec.workload_ids
    ):
        raise _issue(
            "capacity_experiment_contract_changed",
            "experiment is not the expected business-iteration committed_tps selection contract",
            experiment_id=experiment_id,
        )
    objectives = [item for item in spec.objectives if item.metric == CAPACITY_METRIC_ID]
    objective = _one(
        objectives,
        code="capacity_metric_ambiguous",
        message="committed_tps objective must be declared exactly once",
        experiment_id=experiment_id,
    )
    if objective.direction != Direction.MAXIMIZE or objective.unit != CAPACITY_METRIC_UNIT:
        raise _issue(
            "capacity_metric_contract_changed",
            "committed_tps must remain a maximize objective measured in iterations/second",
            experiment_id=experiment_id,
            direction=objective.direction.value,
            unit=objective.unit,
        )
    load_search = spec.scenario.load_search
    if load_search is None or load_search.unit != CAPACITY_METRIC_UNIT:
        raise _issue(
            "capacity_measurement_contract_changed",
            "capacity scenario load-search contract is missing or has a different unit",
            experiment_id=experiment_id,
        )

    binding = spec.input_bindings.get("capacity-config")
    if binding is None or binding.kind != "config" or binding.digest is None:
        raise _issue(
            "capacity_input_contract_changed",
            "capacity-config must be a digest-bound config input",
            experiment_id=experiment_id,
        )
    metadata = binding.metadata
    if binding.digest != canonical_digest(metadata):
        raise _issue(
            "capacity_input_digest_mismatch",
            "capacity-config digest does not match its metadata",
            experiment_id=experiment_id,
        )
    if (
        metadata.get("network") != network
        or metadata.get("sourceDigest") != discovery.source_digest
    ):
        raise _issue(
            "capacity_input_identity_mismatch",
            "capacity input network or source digest does not match the study",
            experiment_id=experiment_id,
            network=network,
        )
    endpoints = metadata.get("endpoints")
    if not isinstance(endpoints, dict) or set(endpoints) != set(active_target_ids):
        raise _issue(
            "capacity_endpoint_identity_mismatch",
            "capacity endpoints must exactly cover the immutable active target set",
            experiment_id=experiment_id,
            endpoint_target_ids=sorted(str(key) for key in endpoints)
            if isinstance(endpoints, dict)
            else [],
            active_target_ids=active_target_ids,
        )

    report = record.report_json
    if report.get("capacityUnit") != CAPACITY_REPORT_UNIT:
        raise _issue(
            "capacity_report_unit_changed",
            "capacity report unit does not match the committed business-iteration contract",
            report_unit=report.get("capacityUnit"),
        )
    report_confidence = report.get("confidenceLevel")
    if canonical_json(report_confidence) != canonical_json(spec.design.confidence_level):
        raise _issue(
            "capacity_confidence_mismatch",
            "capacity report and experiment confidence levels differ",
            report_confidence=report_confidence,
            experiment_confidence=spec.design.confidence_level,
        )
    report_networks = report.get("networks")
    if not isinstance(report_networks, list):
        raise _issue(
            "capacity_report_contract_changed",
            "capacity report does not contain a networks list",
            study_id=record.id,
        )
    report_network = _one(
        [
            item
            for item in report_networks
            if isinstance(item, dict)
            and item.get("network") == network
            and item.get("experimentId") == experiment_id
        ],
        code="capacity_report_network_ambiguous",
        message="capacity report must contain the selected run exactly once",
        study_id=record.id,
        network=network,
        experiment_id=experiment_id,
    )
    selected_frontier = _frontier(
        _report_target(report_network, target_id), target_id=target_id
    )
    control_frontiers = {
        control_id: _frontier(
            _report_target(report_network, control_id), target_id=control_id
        )
        for control_id in active_target_ids
        if control_id != target_id
    }

    evaluation = _evaluation(session, experiment_id, target_id)
    snapshot = evaluation.target_snapshot_json
    if (
        not isinstance(snapshot, dict)
        or snapshot.get("snapshotDigest") != evaluation.target_snapshot_digest
    ):
        raise _issue(
            "capacity_environment_digest_mismatch",
            "evaluation snapshot digest does not match its embedded target snapshot",
            experiment_id=experiment_id,
            target_id=target_id,
        )
    benchmark = _benchmark(session, spec, experiment_id)
    manifest_spec = benchmark.manifest_json.get("spec")
    if not isinstance(manifest_spec, dict):
        raise _issue(
            "capacity_benchmark_contract_changed",
            "capacity benchmark manifest does not contain a spec object",
            benchmark_id=benchmark.benchmark_id,
        )

    draft = record.draft_json
    build_plan = draft.get("build")
    scenario_plan = draft.get("scenario")
    slo_plan = draft.get("slo")
    if not all(isinstance(item, dict) for item in (build_plan, scenario_plan, slo_plan)):
        raise _issue(
            "capacity_draft_contract_changed",
            "capacity draft is missing build, scenario, or SLO contracts",
            study_id=record.id,
        )
    workload_digest = canonical_digest(
        {
            "workloadId": CAPACITY_WORKLOAD_ID,
            "buildPlanDigest": canonical_digest(build_plan),
            "scenarioPlan": scenario_plan,
            "endpoints": endpoints,
            "measurementSeconds": metadata.get("measurementSeconds"),
            "requestTimeoutSeconds": metadata.get("requestTimeoutSeconds"),
        }
    )
    slo_digest = canonical_digest(slo_plan)
    measurement_contract_digest = canonical_digest(
        {
            "objective": objective.model_dump(mode="json"),
            "design": spec.design.model_dump(mode="json"),
            "scenario": spec.scenario.model_dump(mode="json"),
            "adapter": manifest_spec.get("adapter"),
            "runtime": manifest_spec.get("runtime"),
        }
    )
    identity = {
        "source_digest": discovery.source_digest,
        "workload_digest": workload_digest,
        "slo_digest": slo_digest,
        "environment_digest": evaluation.target_snapshot_digest,
        "network": network,
        "target_id": target_id,
        "capacity_unit": CAPACITY_METRIC_UNIT,
        "confidence_level": canonical_json(spec.design.confidence_level),
        "measurement_contract_digest": measurement_contract_digest,
    }

    report_artifact = _put_json(cas, report)
    study_contract = {
        "id": record.id,
        "discoveryId": record.discovery_id,
        "revision": record.revision,
        "draft": record.draft_json,
        "preflight": record.preflight_json,
        "execution": record.execution_json,
        "completedAt": record.completed_at.isoformat(),
    }
    study_contract_artifact = _put_json(cas, study_contract)
    experiment_contract = {
        "id": experiment.id,
        "status": experiment.status,
        "spec": experiment.spec_json,
        "specDigest": experiment.spec_digest,
        "evaluation": {
            "id": evaluation.id,
            "targetId": evaluation.target_id,
            "workloadId": evaluation.workload_id,
            "targetSnapshotDigest": evaluation.target_snapshot_digest,
            "targetSnapshot": evaluation.target_snapshot_json,
        },
    }
    experiment_contract_artifact = _put_json(cas, experiment_contract)

    evidence = CapacityStudyEvidence(
        schema_version=CAPACITY_EVIDENCE_SCHEMA,
        study_id=record.id,
        experiment_id=experiment.id,
        target_id=target_id,
        network=network,
        workload_id=CAPACITY_WORKLOAD_ID,
        metric_id=CAPACITY_METRIC_ID,
        report_digest=report_artifact.digest,
        study_contract_digest=study_contract_artifact.digest,
        experiment_contract_digest=experiment_contract_artifact.digest,
        benchmark_manifest_digest=benchmark.manifest_digest,
        frontier=selected_frontier,
        control_frontiers=control_frontiers,
        active_target_ids=active_target_ids,
        identity=identity,
        context_digest=hypothesis_context_digest(identity),
    )
    normalized_artifact = _put_json(
        cas, evidence.model_dump(mode="json", exclude_none=False)
    )
    manifest = finalize_evidence(
        EvidenceManifest(
            schemaVersion="v1alpha1",
            experimentId=experiment.id,
            evaluationId=evaluation.id,
            benchmarkId=benchmark.benchmark_id,
            benchmarkVersion=benchmark.version,
            benchmarkManifestDigest=benchmark.manifest_digest,
            workloadId=CAPACITY_WORKLOAD_ID,
            environmentDigest=evaluation.target_snapshot_digest,
            rawArtifacts=[
                _artifact(
                    report_artifact,
                    name="capacity-study-report.json",
                    provenance="upstream-output",
                ),
                _artifact(
                    study_contract_artifact,
                    name="capacity-study-contract.json",
                    provenance="upstream-output",
                ),
                _artifact(
                    experiment_contract_artifact,
                    name="capacity-experiment-contract.json",
                    provenance="upstream-output",
                ),
            ],
            normalizedArtifacts=[
                _artifact(
                    normalized_artifact,
                    name="capacity-study-evidence.json",
                    provenance="adapter-generated",
                )
            ],
            adapter=AdapterIdentity(
                adapterId=CAPACITY_EVIDENCE_ADAPTER_ID,
                adapterVersion=CAPACITY_EVIDENCE_ADAPTER_VERSION,
                implementationDigest=CAPACITY_EVIDENCE_IMPLEMENTATION_DIGEST,
                upstreamId=record.id,
                sourceFormat="looper-capacity-study-report/v1alpha1",
                compatibilityStatus="native",
            ),
            createdAt=record.completed_at.isoformat(),
        )
    )
    manifest_artifact = _put_json(
        cas, manifest.model_dump(mode="json", by_alias=True, exclude_none=False)
    )
    return CapacityEvidenceBundle(
        evidence=evidence,
        manifest=manifest,
        report_artifact=report_artifact,
        study_contract_artifact=study_contract_artifact,
        experiment_contract_artifact=experiment_contract_artifact,
        normalized_artifact=normalized_artifact,
        manifest_artifact=manifest_artifact,
    )


__all__ = [
    "CAPACITY_EVIDENCE_ADAPTER_ID",
    "CAPACITY_EVIDENCE_SCHEMA",
    "CapacityEvidenceBundle",
    "CapacityEvidenceError",
    "CapacityEvidenceIssue",
    "CapacityStudyEvidence",
    "ResolvedCapacityFrontier",
    "build_capacity_study_evidence",
]
