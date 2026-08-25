from __future__ import annotations

import json
import math
from enum import StrEnum
from typing import Any, Literal, Protocol

from looper_core.canonical import canonical_json, new_id, utc_now
from looper_core.cas import ArtifactError, FileSystemCAS
from looper_core.contracts import StrictModel
from looper_core.system_opt.config_manifest import ConfigItem, ConfigManifest
from looper_core.system_opt.executor import ExecutorBackend, OperationStatus
from looper_core.system_opt.hypothesis import OptimizationHypothesis
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from looper_api.models import ArtifactRecord
from looper_api.system_optimization_models import (
    SystemOptimizationArtifactLinkRecord,
    SystemOptimizationStudyRecord,
)

SYSTEM_OPTIMIZATION_ORCHESTRATION_SCHEMA = (
    "looper.system-optimization-orchestration/v1alpha1"
)
SYSTEM_OPTIMIZATION_PRODUCER = "looper.system-optimization"
ProblemStage = Literal[
    "draft",
    "hypothesis",
    "approval",
    "apply",
    "capacity",
    "rollback",
    "evaluation",
    "activation",
]


class SystemOptimizationStatus(StrEnum):
    DRAFT = "draft"
    HYPOTHESIS_READY = "hypothesis-ready"
    AWAITING_APPROVAL = "awaiting-approval"
    APPLYING = "applying"
    MEASURING = "measuring"
    ROLLING_BACK = "rolling-back"
    EVALUATING = "evaluating"
    COMPLETED = "completed"
    NEEDS_ATTENTION = "needs-attention"


class CapacityTaskStatus(StrEnum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class SystemOptimizationProblem(StrictModel):
    stage: ProblemStage
    code: str = Field(min_length=1, max_length=120, pattern=r"^[a-z][a-z0-9_]*$")
    message: str = Field(min_length=1, max_length=2000)
    evidence_summary: dict[str, Any] = Field(default_factory=dict)
    suggested_action: str = Field(min_length=1, max_length=2000)


class SystemOptimizationError(RuntimeError):
    def __init__(self, problem: SystemOptimizationProblem) -> None:
        super().__init__(problem.message)
        self.problem = problem


class CapacityTaskObservation(StrictModel):
    capacity_study_id: str = Field(min_length=1, max_length=80)
    status: CapacityTaskStatus
    report_digest: str | None = Field(
        default=None, pattern=r"^sha256:[0-9a-f]{64}$"
    )
    error_code: str | None = Field(default=None, min_length=1, max_length=120)
    evidence: dict[str, Any] = Field(default_factory=dict)


class StudyArtifactReference(StrictModel):
    digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    size: int = Field(ge=0)
    role: str = Field(min_length=1, max_length=40)
    name: str = Field(min_length=1, max_length=255)


class StudyEvaluationResult(StrictModel):
    outcome: Literal["accepted", "rejected", "inconclusive", "blocked"]
    decision: dict[str, Any]
    artifacts: list[StudyArtifactReference] = Field(default_factory=list)


class ReconcileResult(StrictModel):
    study_id: str
    previous_status: SystemOptimizationStatus
    status: SystemOptimizationStatus
    changed: bool
    external_action: str | None = None
    requires_commit_before_next: bool


class CapacityStudyDriver(Protocol):
    def submit_candidate(
        self,
        *,
        baseline_capacity_study_id: str,
        target_id: str,
        network: str,
        hypothesis_digest: str,
        idempotency_key: str,
    ) -> str: ...

    def observe(self, capacity_study_id: str) -> CapacityTaskObservation: ...


class StudyEvaluator(Protocol):
    def evaluate(
        self,
        study: SystemOptimizationStudyRecord,
        observation: CapacityTaskObservation,
    ) -> StudyEvaluationResult: ...


def _problem(
    stage: ProblemStage,
    code: str,
    message: str,
    *,
    suggested_action: str,
    **evidence: Any,
) -> SystemOptimizationProblem:
    return SystemOptimizationProblem(
        stage=stage,
        code=code,
        message=message,
        evidence_summary=evidence,
        suggested_action=suggested_action,
    )


def _raise_problem(problem: SystemOptimizationProblem) -> None:
    raise SystemOptimizationError(problem)


def _status(record: SystemOptimizationStudyRecord) -> SystemOptimizationStatus:
    try:
        return SystemOptimizationStatus(record.status)
    except ValueError as error:
        _raise_problem(
            _problem(
                "evaluation",
                "unknown_study_status",
                "stored optimization study has an unknown status",
                suggested_action="Stop the worker and inspect the persisted record.",
                status=record.status,
            )
        )
        raise AssertionError from error


def _touch(record: SystemOptimizationStudyRecord) -> None:
    record.revision += 1
    record.updated_at = utc_now()


def _set_problem(
    record: SystemOptimizationStudyRecord, problem: SystemOptimizationProblem
) -> None:
    record.problem_json = problem.model_dump(mode="json", exclude_none=False)


def _assert_revision(record: SystemOptimizationStudyRecord, expected_revision: int) -> None:
    if record.revision != expected_revision:
        _raise_problem(
            _problem(
                "approval",
                "revision_conflict",
                "optimization study revision has changed",
                suggested_action="Reload the study and review the current evidence digests.",
                expected_revision=expected_revision,
                actual_revision=record.revision,
            )
        )


def _assert_writes_allowed(record: SystemOptimizationStudyRecord) -> None:
    if _status(record) == SystemOptimizationStatus.NEEDS_ATTENTION:
        _raise_problem(
            _problem(
                "rollback",
                "writes_blocked_after_rollback_failure",
                "further target writes are blocked because rollback was not verified",
                suggested_action=(
                    "Recover the target out of band, verify the original value, and create a "
                    "new study."
                ),
                study_id=record.id,
                problem=record.problem_json,
            )
        )


def _put_artifact(
    session: Session,
    cas: FileSystemCAS,
    record: SystemOptimizationStudyRecord,
    *,
    role: str,
    name: str,
    payload: Any,
) -> str:
    encoded = canonical_json(payload).encode("utf-8")
    stored = cas.put_bytes(encoded)
    artifact = session.get(ArtifactRecord, stored.digest)
    if artifact is None:
        artifact = ArtifactRecord(
            digest=stored.digest,
            size=stored.size,
            verified=True,
            created_at=utc_now(),
        )
        session.add(artifact)
        session.flush()
    elif artifact.size != stored.size or not artifact.verified:
        _raise_problem(
            _problem(
                "evaluation",
                "artifact_identity_conflict",
                "existing artifact metadata does not match the CAS blob",
                suggested_action="Stop reconciliation and audit the artifact store.",
                digest=stored.digest,
                expected_size=stored.size,
                actual_size=artifact.size,
                verified=artifact.verified,
            )
        )
    link = session.scalar(
        select(SystemOptimizationArtifactLinkRecord).where(
            SystemOptimizationArtifactLinkRecord.study_id == record.id,
            SystemOptimizationArtifactLinkRecord.digest == stored.digest,
            SystemOptimizationArtifactLinkRecord.role == role,
            SystemOptimizationArtifactLinkRecord.name == name,
        )
    )
    if link is None:
        session.add(
            SystemOptimizationArtifactLinkRecord(
                id=new_id("soalink"),
                study_id=record.id,
                digest=stored.digest,
                role=role,
                name=name,
                media_type="application/json",
                producer=SYSTEM_OPTIMIZATION_PRODUCER,
                created_at=utc_now(),
            )
        )
        session.flush()
    return stored.digest


def _read_json(cas: FileSystemCAS, digest: str) -> Any:
    try:
        verified = cas.verify(digest)
        with verified.path.open("r", encoding="utf-8") as source:
            return json.load(source)
    except (ArtifactError, OSError, ValueError, json.JSONDecodeError) as error:
        _raise_problem(
            _problem(
                "rollback",
                "required_artifact_unavailable",
                "a digest-bound artifact required for safe reconciliation is unavailable",
                suggested_action="Restore the verified CAS blob before resuming the study.",
                digest=digest,
                error=type(error).__name__,
            )
        )
        raise AssertionError from error


def _link_existing_artifact(
    session: Session,
    cas: FileSystemCAS,
    record: SystemOptimizationStudyRecord,
    reference: StudyArtifactReference,
) -> None:
    stored = cas.verify(reference.digest, expected_size=reference.size)
    artifact = session.get(ArtifactRecord, reference.digest)
    if artifact is None:
        session.add(
            ArtifactRecord(
                digest=reference.digest,
                size=stored.size,
                verified=True,
                created_at=utc_now(),
            )
        )
        session.flush()
    elif artifact.size != stored.size or not artifact.verified:
        _raise_problem(
            _problem(
                "evaluation",
                "artifact_identity_conflict",
                "referenced evidence metadata does not match the verified CAS blob",
                suggested_action="Stop reconciliation and audit the artifact store.",
                digest=reference.digest,
            )
        )
    link = session.scalar(
        select(SystemOptimizationArtifactLinkRecord).where(
            SystemOptimizationArtifactLinkRecord.study_id == record.id,
            SystemOptimizationArtifactLinkRecord.digest == reference.digest,
            SystemOptimizationArtifactLinkRecord.role == reference.role,
            SystemOptimizationArtifactLinkRecord.name == reference.name,
        )
    )
    if link is None:
        session.add(
            SystemOptimizationArtifactLinkRecord(
                id=new_id("soalink"),
                study_id=record.id,
                digest=reference.digest,
                role=reference.role,
                name=reference.name,
                media_type="application/json",
                producer=SYSTEM_OPTIMIZATION_PRODUCER,
                created_at=utc_now(),
            )
        )


def create_system_optimization_study(
    session: Session,
    *,
    baseline_capacity_study_id: str,
    target_id: str,
    network: Literal["internal", "external"],
    minimum_effect: float,
    authorization_profile_digest: str,
    allowed_config_item_ids: list[str] | None = None,
) -> SystemOptimizationStudyRecord:
    if not math.isfinite(minimum_effect) or minimum_effect < 0:
        _raise_problem(
            _problem(
                "draft",
                "minimum_effect_invalid",
                "minimumEffect must be finite and non-negative",
                suggested_action="Submit an explicit non-negative fractional effect.",
                minimum_effect=minimum_effect,
            )
        )
    if not re_full_digest(authorization_profile_digest):
        _raise_problem(
            _problem(
                "draft",
                "authorization_profile_digest_invalid",
                "authorization profile digest is missing or invalid",
                suggested_action="Resolve and submit the exact authorization profile digest.",
            )
        )
    now = utc_now()
    record = SystemOptimizationStudyRecord(
        id=new_id("sysopt"),
        baseline_capacity_study_id=baseline_capacity_study_id,
        candidate_capacity_study_id=None,
        target_id=target_id,
        network=network,
        minimum_effect=minimum_effect,
        authorization_profile_digest=authorization_profile_digest,
        status=SystemOptimizationStatus.DRAFT.value,
        revision=1,
        fencing_token=1,
        hypothesis_digest=None,
        decision_digest=None,
        snapshot_digest=None,
        rollback_verified=False,
        orchestration_json={
            "schemaVersion": SYSTEM_OPTIMIZATION_ORCHESTRATION_SCHEMA,
            "apply": {},
            "capacity": {},
            "allowed_config_items": allowed_config_item_ids or [],
            "rollback": {},
            "evaluation": {},
        },
        activation_json={},
        problem_json=None,
        created_at=now,
        updated_at=now,
        approved_at=None,
        completed_at=None,
    )
    session.add(record)
    session.flush()
    return record


def re_full_digest(value: str) -> bool:
    if not value.startswith("sha256:") or len(value) != 71:
        return False
    return all(character in "0123456789abcdef" for character in value[7:])


def record_optimization_hypothesis(
    session: Session,
    cas: FileSystemCAS,
    record: SystemOptimizationStudyRecord,
    hypothesis: OptimizationHypothesis,
    *,
    expected_revision: int,
) -> SystemOptimizationStudyRecord:
    _assert_writes_allowed(record)
    _assert_revision(record, expected_revision)
    if _status(record) != SystemOptimizationStatus.DRAFT:
        _raise_problem(
            _problem(
                "hypothesis",
                "hypothesis_transition_invalid",
                "a hypothesis can only be attached to a draft study",
                suggested_action="Reload the study and follow its current state.",
                status=record.status,
            )
        )
    digest = _put_artifact(
        session,
        cas,
        record,
        role="hypothesis",
        name="optimization-hypothesis.json",
        payload=hypothesis.model_dump(mode="json", exclude_none=False),
    )
    if digest != hypothesis.digest:
        _raise_problem(
            _problem(
                "hypothesis",
                "hypothesis_digest_mismatch",
                "serialized hypothesis does not match its declared canonical digest",
                suggested_action="Stop and audit hypothesis serialization.",
                artifact_digest=digest,
                hypothesis_digest=hypothesis.digest,
            )
        )
    record.hypothesis_digest = digest
    record.status = SystemOptimizationStatus.HYPOTHESIS_READY.value
    _touch(record)
    return record


def request_optimization_approval(
    record: SystemOptimizationStudyRecord, *, expected_revision: int
) -> SystemOptimizationStudyRecord:
    _assert_writes_allowed(record)
    _assert_revision(record, expected_revision)
    if _status(record) != SystemOptimizationStatus.HYPOTHESIS_READY:
        _raise_problem(
            _problem(
                "approval",
                "approval_transition_invalid",
                "only a hypothesis-ready study can await approval",
                suggested_action="Finish evidence generation before requesting approval.",
                status=record.status,
            )
        )
    record.status = SystemOptimizationStatus.AWAITING_APPROVAL.value
    _touch(record)
    return record


def approve_optimization_study(
    record: SystemOptimizationStudyRecord,
    *,
    hypothesis_digest: str,
    expected_revision: int,
) -> SystemOptimizationStudyRecord:
    _assert_writes_allowed(record)
    _assert_revision(record, expected_revision)
    if _status(record) != SystemOptimizationStatus.AWAITING_APPROVAL:
        _raise_problem(
            _problem(
                "approval",
                "approval_transition_invalid",
                "study is not awaiting operator approval",
                suggested_action="Reload the study and review its current state.",
                status=record.status,
            )
        )
    if hypothesis_digest != record.hypothesis_digest:
        _raise_problem(
            _problem(
                "approval",
                "hypothesis_digest_mismatch",
                "approved hypothesis digest does not match the persisted candidate",
                suggested_action="Review and approve the exact current hypothesis digest.",
                expected=record.hypothesis_digest,
                actual=hypothesis_digest,
            )
        )
    record.status = SystemOptimizationStatus.APPLYING.value
    orchestration = dict(record.orchestration_json)
    orchestration["apply"] = {"phase": "snapshot-pending"}
    record.orchestration_json = orchestration
    record.approved_at = utc_now()
    _touch(record)
    return record


def _load_hypothesis(
    record: SystemOptimizationStudyRecord, cas: FileSystemCAS
) -> OptimizationHypothesis:
    if record.hypothesis_digest is None:
        _raise_problem(
            _problem(
                "hypothesis",
                "hypothesis_missing",
                "study cannot reconcile without a persisted hypothesis digest",
                suggested_action="Stop the study and regenerate its hypothesis evidence.",
            )
        )
    try:
        return OptimizationHypothesis.model_validate(
            _read_json(cas, record.hypothesis_digest)
        )
    except ValueError as error:
        _raise_problem(
            _problem(
                "hypothesis",
                "hypothesis_artifact_invalid",
                "persisted hypothesis artifact no longer validates",
                suggested_action="Restore the original verified hypothesis artifact.",
                digest=record.hypothesis_digest,
            )
        )
        raise AssertionError from error


def _candidate(
    hypothesis: OptimizationHypothesis, manifest: ConfigManifest
) -> tuple[ConfigItem, Any]:
    if len(hypothesis.candidate_parameters) != 1:
        _raise_problem(
            _problem(
                "apply",
                "candidate_cardinality_invalid",
                "the first implementation requires exactly one candidate parameter",
                suggested_action="Generate a single-parameter hypothesis.",
                parameter_count=len(hypothesis.candidate_parameters),
            )
        )
    parameter_id, value = next(iter(hypothesis.candidate_parameters.items()))
    try:
        item = manifest.item_for_parameter(parameter_id)
        item.validate_value(value)
    except (KeyError, ValueError) as error:
        _raise_problem(
            _problem(
                "apply",
                "candidate_contract_invalid",
                "candidate no longer matches the supplied configuration manifest",
                suggested_action="Stop and regenerate against the current authorization domain.",
                parameter_id=parameter_id,
            )
        )
        raise AssertionError from error
    return item, value


def _transition_to_rollback(
    record: SystemOptimizationStudyRecord,
    problem: SystemOptimizationProblem,
    *,
    operation: dict[str, Any] | None = None,
) -> None:
    orchestration = dict(record.orchestration_json)
    apply_data = dict(orchestration.get("apply") or {})
    if operation is not None:
        apply_data["lastOperation"] = operation
    orchestration["apply"] = apply_data
    orchestration["rollback"] = {"phase": "pending"}
    record.orchestration_json = orchestration
    record.status = SystemOptimizationStatus.ROLLING_BACK.value
    _set_problem(record, problem)
    _touch(record)


def _snapshot_step(
    session: Session,
    cas: FileSystemCAS,
    record: SystemOptimizationStudyRecord,
    backend: ExecutorBackend,
    item: ConfigItem,
) -> str:
    snapshot = backend.snapshot([item], fencing_token=record.fencing_token)
    if not snapshot.complete:
        problem = _problem(
            "apply",
            "snapshot_incomplete",
            "target snapshot is incomplete; no configuration was applied",
            suggested_action="Restore target observability before starting a new study.",
            snapshot=snapshot.model_dump(mode="json", exclude_none=False),
        )
        record.status = SystemOptimizationStatus.NEEDS_ATTENTION.value
        _set_problem(record, problem)
        _touch(record)
        return "snapshot-failed"
    payload = snapshot.model_dump(mode="json", exclude_none=True)
    digest = _put_artifact(
        session,
        cas,
        record,
        role="snapshot",
        name="pre-experiment-snapshot.json",
        payload=payload,
    )
    if digest != snapshot.digest:
        _raise_problem(
            _problem(
                "apply",
                "snapshot_digest_mismatch",
                "snapshot artifact digest does not match the executor snapshot",
                suggested_action="Stop reconciliation and audit snapshot serialization.",
                artifact_digest=digest,
                snapshot_digest=snapshot.digest,
            )
        )
    orchestration = dict(record.orchestration_json)
    orchestration["apply"] = {"phase": "snapshot-ready"}
    record.orchestration_json = orchestration
    record.snapshot_digest = digest
    _touch(record)
    return "snapshot"


def _apply_step(
    record: SystemOptimizationStudyRecord,
    backend: ExecutorBackend,
    item: ConfigItem,
    value: Any,
) -> str:
    probe = backend.probe(item, fencing_token=record.fencing_token)
    if probe.status == OperationStatus.SUCCEEDED and canonical_json(probe.value) == canonical_json(
        value
    ):
        operation = backend.verify(item, value, fencing_token=record.fencing_token)
        action = "verify-existing-apply"
    else:
        operation = backend.apply(item, value, fencing_token=record.fencing_token)
        action = "apply"
        if operation.succeeded:
            operation = backend.verify(item, value, fencing_token=record.fencing_token)
            action = "apply-and-verify"
    serialized = operation.model_dump(mode="json", exclude_none=False)
    if not operation.succeeded:
        _transition_to_rollback(
            record,
            _problem(
                "apply",
                "apply_or_verify_failed",
                "candidate configuration could not be applied and verified",
                suggested_action="Allow reconciliation to restore the persisted snapshot.",
                operation=serialized,
            ),
            operation=serialized,
        )
        return action
    orchestration = dict(record.orchestration_json)
    orchestration["apply"] = {"phase": "verified", "lastOperation": serialized}
    orchestration["capacity"] = {"phase": "submission-pending"}
    record.orchestration_json = orchestration
    record.status = SystemOptimizationStatus.MEASURING.value
    _touch(record)
    return action


def _measurement_step(
    record: SystemOptimizationStudyRecord,
    driver: CapacityStudyDriver,
) -> tuple[bool, str | None]:
    orchestration = dict(record.orchestration_json)
    capacity = dict(orchestration.get("capacity") or {})
    if record.candidate_capacity_study_id is None:
        assert record.hypothesis_digest is not None
        candidate_id = driver.submit_candidate(
            baseline_capacity_study_id=record.baseline_capacity_study_id,
            target_id=record.target_id,
            network=record.network,
            hypothesis_digest=record.hypothesis_digest,
            idempotency_key=f"system-optimization:{record.id}",
        )
        record.candidate_capacity_study_id = candidate_id
        capacity.update({"phase": "submitted", "capacityStudyId": candidate_id})
        orchestration["capacity"] = capacity
        record.orchestration_json = orchestration
        _touch(record)
        return True, "submit-capacity"
    observation = driver.observe(record.candidate_capacity_study_id)
    if observation.capacity_study_id != record.candidate_capacity_study_id:
        _raise_problem(
            _problem(
                "capacity",
                "capacity_observation_identity_mismatch",
                "capacity observer returned a different task identity",
                suggested_action="Stop the worker and audit the capacity adapter.",
                expected=record.candidate_capacity_study_id,
                actual=observation.capacity_study_id,
            )
        )
    if observation.status == CapacityTaskStatus.PENDING:
        return False, None
    capacity["phase"] = "terminal"
    capacity["observation"] = observation.model_dump(mode="json", exclude_none=False)
    orchestration["capacity"] = capacity
    orchestration["rollback"] = {"phase": "pending"}
    record.orchestration_json = orchestration
    record.status = SystemOptimizationStatus.ROLLING_BACK.value
    if observation.status != CapacityTaskStatus.COMPLETED:
        _set_problem(
            record,
            _problem(
                "capacity",
                "candidate_capacity_failed",
                "candidate capacity task did not complete successfully",
                suggested_action="Restore the target first; then inspect capacity task evidence.",
                observation=observation.model_dump(mode="json", exclude_none=False),
            ),
        )
    _touch(record)
    return True, "observe-capacity-terminal"


def _rollback_step(
    record: SystemOptimizationStudyRecord,
    cas: FileSystemCAS,
    backend: ExecutorBackend,
    item: ConfigItem,
) -> str:
    if record.snapshot_digest is None:
        problem = _problem(
            "rollback",
            "rollback_snapshot_missing",
            "rollback cannot proceed without the persisted pre-experiment snapshot",
            suggested_action="Recover the target out of band and verify its original value.",
        )
        record.status = SystemOptimizationStatus.NEEDS_ATTENTION.value
        _set_problem(record, problem)
        _touch(record)
        return "rollback-blocked"
    raw_snapshot = _read_json(cas, record.snapshot_digest)
    try:
        entry = raw_snapshot["entries"][item.id]
        snapshot_value = entry["value"]
    except (KeyError, TypeError):
        problem = _problem(
            "rollback",
            "rollback_snapshot_invalid",
            "snapshot artifact does not contain the candidate item",
            suggested_action="Recover the target out of band and audit the snapshot artifact.",
            item_id=item.id,
            snapshot_digest=record.snapshot_digest,
        )
        record.status = SystemOptimizationStatus.NEEDS_ATTENTION.value
        _set_problem(record, problem)
        _touch(record)
        return "rollback-blocked"
    probe = backend.probe(item, fencing_token=record.fencing_token)
    if probe.status == OperationStatus.SUCCEEDED and canonical_json(probe.value) == canonical_json(
        snapshot_value
    ):
        operation = backend.verify(item, snapshot_value, fencing_token=record.fencing_token)
        action = "verify-existing-rollback"
    else:
        operation = backend.rollback(
            item, snapshot_value, fencing_token=record.fencing_token
        )
        action = "rollback"
        if operation.succeeded:
            operation = backend.verify(
                item, snapshot_value, fencing_token=record.fencing_token
            )
            action = "rollback-and-verify"
    serialized = operation.model_dump(mode="json", exclude_none=False)
    orchestration = dict(record.orchestration_json)
    rollback = dict(orchestration.get("rollback") or {})
    rollback.update({"phase": "verified" if operation.succeeded else "failed"})
    rollback["lastOperation"] = serialized
    orchestration["rollback"] = rollback
    record.orchestration_json = orchestration
    if not operation.succeeded:
        record.rollback_verified = False
        record.status = SystemOptimizationStatus.NEEDS_ATTENTION.value
        _set_problem(
            record,
            _problem(
                "rollback",
                "rollback_verification_failed",
                "original target configuration was not restored and verified",
                suggested_action=(
                    "Block all further writes and recover the target through an out-of-band "
                    "operator channel."
                ),
                operation=serialized,
                snapshot_digest=record.snapshot_digest,
            ),
        )
    else:
        record.rollback_verified = True
        record.status = SystemOptimizationStatus.EVALUATING.value
    _touch(record)
    return action


def _evaluation_step(
    session: Session,
    cas: FileSystemCAS,
    record: SystemOptimizationStudyRecord,
    driver: CapacityStudyDriver,
    evaluator: StudyEvaluator,
) -> str:
    if not record.rollback_verified:
        _raise_problem(
            _problem(
                "evaluation",
                "evaluation_before_rollback",
                "benefit evaluation is forbidden until rollback is verified",
                suggested_action="Complete and verify rollback first.",
            )
        )
    observation = (
        driver.observe(record.candidate_capacity_study_id)
        if record.candidate_capacity_study_id is not None
        else None
    )
    if observation is None:
        result = StudyEvaluationResult(
            outcome="blocked",
            decision={
                "schemaVersion": "looper.blocked-optimization-decision/v1alpha1",
                "reason": "candidate configuration failed before capacity submission",
                "rollbackVerified": True,
            },
        )
    elif observation.status != CapacityTaskStatus.COMPLETED:
        result = StudyEvaluationResult(
            outcome="blocked",
            decision={
                "schemaVersion": "looper.blocked-optimization-decision/v1alpha1",
                "reason": "candidate capacity task was not completed",
                "capacityObservation": observation.model_dump(
                    mode="json", exclude_none=False
                ),
                "rollbackVerified": True,
            },
        )
    else:
        result = evaluator.evaluate(record, observation)
    for reference in result.artifacts:
        _link_existing_artifact(session, cas, record, reference)
    digest = _put_artifact(
        session,
        cas,
        record,
        role="decision",
        name="capacity-benefit-decision.json",
        payload=result.decision,
    )
    orchestration = dict(record.orchestration_json)
    orchestration["evaluation"] = {
        "phase": "completed",
        "outcome": result.outcome,
        "decisionDigest": digest,
    }
    record.orchestration_json = orchestration
    record.decision_digest = digest
    record.status = SystemOptimizationStatus.COMPLETED.value
    record.completed_at = utc_now()
    _touch(record)
    return "evaluate"


def reconcile_system_optimization_study(
    session: Session,
    cas: FileSystemCAS,
    record: SystemOptimizationStudyRecord,
    *,
    backend: ExecutorBackend,
    manifest: ConfigManifest,
    capacity_driver: CapacityStudyDriver,
    evaluator: StudyEvaluator,
) -> ReconcileResult:
    previous = _status(record)
    if previous in {
        SystemOptimizationStatus.DRAFT,
        SystemOptimizationStatus.HYPOTHESIS_READY,
        SystemOptimizationStatus.AWAITING_APPROVAL,
        SystemOptimizationStatus.COMPLETED,
        SystemOptimizationStatus.NEEDS_ATTENTION,
    }:
        return ReconcileResult(
            study_id=record.id,
            previous_status=previous,
            status=previous,
            changed=False,
            requires_commit_before_next=False,
        )
    _assert_writes_allowed(record)
    hypothesis = _load_hypothesis(record, cas)
    item, value = _candidate(hypothesis, manifest)
    action: str | None = None
    changed = True
    if previous == SystemOptimizationStatus.APPLYING:
        apply_data = record.orchestration_json.get("apply") or {}
        if record.snapshot_digest is None or apply_data.get("phase") == "snapshot-pending":
            action = _snapshot_step(session, cas, record, backend, item)
        else:
            action = _apply_step(record, backend, item, value)
    elif previous == SystemOptimizationStatus.MEASURING:
        changed, action = _measurement_step(record, capacity_driver)
    elif previous == SystemOptimizationStatus.ROLLING_BACK:
        action = _rollback_step(record, cas, backend, item)
    elif previous == SystemOptimizationStatus.EVALUATING:
        action = _evaluation_step(
            session, cas, record, capacity_driver, evaluator
        )
    else:  # pragma: no cover - enum exhaustiveness guard
        raise AssertionError(previous)
    return ReconcileResult(
        study_id=record.id,
        previous_status=previous,
        status=_status(record),
        changed=changed,
        external_action=action,
        requires_commit_before_next=changed,
    )


def recover_interrupted_system_optimization_studies(session: Session) -> int:
    recoverable = {
        SystemOptimizationStatus.APPLYING.value,
        SystemOptimizationStatus.MEASURING.value,
        SystemOptimizationStatus.ROLLING_BACK.value,
        SystemOptimizationStatus.EVALUATING.value,
    }
    records = list(
        session.scalars(
            select(SystemOptimizationStudyRecord).where(
                SystemOptimizationStudyRecord.status.in_(recoverable)
            )
        )
    )
    for record in records:
        record.fencing_token += 1
        _touch(record)
    return len(records)


def halt_system_optimization_study(
    record: SystemOptimizationStudyRecord, problem: SystemOptimizationProblem
) -> SystemOptimizationStudyRecord:
    record.status = SystemOptimizationStatus.NEEDS_ATTENTION.value
    _set_problem(record, problem)
    _touch(record)
    return record


def report_system_optimization_problem(
    record: SystemOptimizationStudyRecord, problem: SystemOptimizationProblem
) -> SystemOptimizationStudyRecord:
    _set_problem(record, problem)
    _touch(record)
    return record


def persist_system_optimization_artifact(
    session: Session,
    cas: FileSystemCAS,
    record: SystemOptimizationStudyRecord,
    *,
    role: str,
    name: str,
    payload: Any,
) -> str:
    return _put_artifact(
        session, cas, record, role=role, name=name, payload=payload
    )


def link_system_optimization_artifact(
    session: Session,
    cas: FileSystemCAS,
    record: SystemOptimizationStudyRecord,
    reference: StudyArtifactReference,
) -> None:
    _link_existing_artifact(session, cas, record, reference)


def _validate_activation_context(
    record: SystemOptimizationStudyRecord,
    decision: dict[str, Any],
    *,
    current_environment_digest: str,
    current_authorization_profile_digest: str,
    backend: ExecutorBackend,
) -> None:
    if decision.get("status") != "accepted" or decision.get("rollbackVerified") is not True:
        _raise_problem(
            _problem(
                "activation",
                "decision_not_accepted",
                "only an accepted decision with verified experimental rollback can activate",
                suggested_action="Run a comparable controlled capacity experiment first.",
                decision_status=decision.get("status"),
                rollback_verified=decision.get("rollbackVerified"),
            )
        )
    identity = decision.get("measurementIdentity")
    expected_environment = (
        identity.get("environment_digest") if isinstance(identity, dict) else None
    )
    if not expected_environment or current_environment_digest != expected_environment:
        _raise_problem(
            _problem(
                "activation",
                "activation_environment_drift",
                "target environment no longer matches the accepted measurement identity",
                suggested_action=(
                    "Run a new baseline and candidate study in the current environment."
                ),
                expected=expected_environment,
                actual=current_environment_digest,
            )
        )
    if current_authorization_profile_digest != record.authorization_profile_digest:
        _raise_problem(
            _problem(
                "activation",
                "activation_authorization_drift",
                "authorization profile changed after the experiment",
                suggested_action="Review current authorization and run a new study.",
                expected=record.authorization_profile_digest,
                actual=current_authorization_profile_digest,
            )
        )
    if (
        not backend.capabilities.enabled
        or backend.capabilities.target_id != record.target_id
    ):
        _raise_problem(
            _problem(
                "activation",
                "activation_backend_identity_mismatch",
                "activation backend is disabled or bound to a different target",
                suggested_action="Reconnect the exact digest-bound target executor.",
                expected_target_id=record.target_id,
                actual_target_id=backend.capabilities.target_id,
                enabled=backend.capabilities.enabled,
            )
        )


def _snapshot_item_value(snapshot: Any, item_id: str) -> Any:
    try:
        return snapshot["entries"][item_id]["value"]
    except (KeyError, TypeError) as error:
        _raise_problem(
            _problem(
                "activation",
                "activation_snapshot_invalid",
                "activation cannot resolve the exact saved value for the candidate item",
                suggested_action="Restore the verified snapshot artifact before continuing.",
                item_id=item_id,
            )
        )
        raise AssertionError from error


def prepare_optimization_activation(
    session: Session,
    cas: FileSystemCAS,
    record: SystemOptimizationStudyRecord,
    *,
    decision_digest: str,
    expected_revision: int,
    current_environment_digest: str,
    current_authorization_profile_digest: str,
    backend: ExecutorBackend,
    manifest: ConfigManifest,
) -> SystemOptimizationStudyRecord:
    _assert_writes_allowed(record)
    _assert_revision(record, expected_revision)
    if _status(record) != SystemOptimizationStatus.COMPLETED:
        _raise_problem(
            _problem(
                "activation",
                "activation_transition_invalid",
                "only a completed optimization study can be activated",
                suggested_action="Wait for rollback and benefit evaluation to complete.",
                status=record.status,
            )
        )
    if decision_digest != record.decision_digest:
        _raise_problem(
            _problem(
                "activation",
                "activation_decision_digest_mismatch",
                "activation request does not reference the current decision digest",
                suggested_action="Review and submit the exact accepted decision digest.",
                expected=record.decision_digest,
                actual=decision_digest,
            )
        )
    if record.activation_json.get("status") == "active":
        _raise_problem(
            _problem(
                "activation",
                "activation_already_active",
                "the accepted runtime configuration is already active",
                suggested_action="Use explicit rollback before requesting another activation.",
            )
        )
    if record.activation_json.get("phase") == "snapshot-ready":
        _raise_problem(
            _problem(
                "activation",
                "activation_already_prepared",
                "activation snapshot is already committed",
                suggested_action="Apply the prepared activation using its current revision.",
            )
        )
    decision = _read_json(cas, decision_digest)
    if not isinstance(decision, dict):
        _raise_problem(
            _problem(
                "activation",
                "activation_decision_invalid",
                "accepted decision artifact is not a JSON object",
                suggested_action="Restore the verified decision artifact.",
            )
        )
    _validate_activation_context(
        record,
        decision,
        current_environment_digest=current_environment_digest,
        current_authorization_profile_digest=current_authorization_profile_digest,
        backend=backend,
    )
    if record.activation_json.get("phase") != "fenced":
        record.fencing_token += 1
        record.activation_json = {
            "status": "pending",
            "phase": "fenced",
            "decisionDigest": decision_digest,
            "environmentDigest": current_environment_digest,
            "authorizationProfileDigest": current_authorization_profile_digest,
            "runtimeOnly": True,
            "persistentConfigurationWritten": False,
        }
        _touch(record)
        return record
    if record.activation_json.get("decisionDigest") != decision_digest:
        _raise_problem(
            _problem(
                "activation",
                "activation_fence_identity_mismatch",
                "prepared activation fence references a different decision",
                suggested_action="Stop and inspect the persisted activation state.",
            )
        )
    hypothesis = _load_hypothesis(record, cas)
    item, _value = _candidate(hypothesis, manifest)
    preflight = backend.preflight_check(item)
    if not preflight.succeeded:
        _raise_problem(
            _problem(
                "activation",
                "activation_preflight_failed",
                "target no longer satisfies the restricted executor preflight",
                suggested_action=(
                    "Restore privilege and target compatibility, then re-evaluate drift."
                ),
                preflight=preflight.model_dump(mode="json", exclude_none=False),
            )
        )
    snapshot = backend.snapshot([item], fencing_token=record.fencing_token)
    if not snapshot.complete:
        _raise_problem(
            _problem(
                "activation",
                "activation_snapshot_incomplete",
                "activation snapshot could not be read completely",
                suggested_action="Restore target observability before activation.",
                snapshot=snapshot.model_dump(mode="json", exclude_none=False),
            )
        )
    if record.snapshot_digest is None:
        _raise_problem(
            _problem(
                "activation",
                "experimental_snapshot_missing",
                "accepted study has no pre-experiment snapshot for drift comparison",
                suggested_action="Do not activate; run a new complete experiment.",
            )
        )
    experimental_snapshot = _read_json(cas, record.snapshot_digest)
    experimental_value = _snapshot_item_value(experimental_snapshot, item.id)
    current_value = snapshot.entries[item.id].value
    if canonical_json(current_value) != canonical_json(experimental_value):
        _raise_problem(
            _problem(
                "activation",
                "activation_configuration_drift",
                "current configuration differs from the verified pre-experiment snapshot",
                suggested_action="Resolve target drift and run a new experiment before activation.",
                item_id=item.id,
                expected=experimental_value,
                actual=current_value,
            )
        )
    payload = snapshot.model_dump(mode="json", exclude_none=True)
    activation_snapshot_digest = _put_artifact(
        session,
        cas,
        record,
        role="activation-snapshot",
        name="pre-activation-snapshot.json",
        payload=payload,
    )
    if activation_snapshot_digest != snapshot.digest:
        _raise_problem(
            _problem(
                "activation",
                "activation_snapshot_digest_mismatch",
                "activation snapshot serialization changed its digest",
                suggested_action="Stop and audit activation snapshot persistence.",
            )
        )
    record.activation_json = {
        "status": "pending",
        "phase": "snapshot-ready",
        "decisionDigest": decision_digest,
        "snapshotDigest": activation_snapshot_digest,
        "environmentDigest": current_environment_digest,
        "authorizationProfileDigest": current_authorization_profile_digest,
        "runtimeOnly": True,
        "persistentConfigurationWritten": False,
    }
    _touch(record)
    return record


def apply_prepared_optimization_activation(
    session: Session,
    cas: FileSystemCAS,
    record: SystemOptimizationStudyRecord,
    *,
    expected_revision: int,
    current_environment_digest: str,
    current_authorization_profile_digest: str,
    backend: ExecutorBackend,
    manifest: ConfigManifest,
) -> SystemOptimizationStudyRecord:
    _assert_writes_allowed(record)
    _assert_revision(record, expected_revision)
    activation = dict(record.activation_json)
    if activation.get("phase") != "snapshot-ready":
        _raise_problem(
            _problem(
                "activation",
                "activation_not_prepared",
                "activation requires a committed pre-activation snapshot",
                suggested_action="Prepare activation and commit its snapshot first.",
                phase=activation.get("phase"),
            )
        )
    decision = _read_json(cas, str(activation["decisionDigest"]))
    if not isinstance(decision, dict):
        _raise_problem(
            _problem(
                "activation",
                "activation_decision_invalid",
                "accepted decision artifact is not a JSON object",
                suggested_action="Restore the verified decision artifact.",
            )
        )
    _validate_activation_context(
        record,
        decision,
        current_environment_digest=current_environment_digest,
        current_authorization_profile_digest=current_authorization_profile_digest,
        backend=backend,
    )
    hypothesis = _load_hypothesis(record, cas)
    item, value = _candidate(hypothesis, manifest)
    probe = backend.probe(item, fencing_token=record.fencing_token)
    if probe.status == OperationStatus.SUCCEEDED and canonical_json(probe.value) == canonical_json(
        value
    ):
        operation = backend.verify(item, value, fencing_token=record.fencing_token)
    else:
        operation = backend.apply(item, value, fencing_token=record.fencing_token)
        if operation.succeeded:
            operation = backend.verify(item, value, fencing_token=record.fencing_token)
    if not operation.succeeded:
        snapshot = _read_json(cas, str(activation["snapshotDigest"]))
        original = _snapshot_item_value(snapshot, item.id)
        rollback = backend.rollback(item, original, fencing_token=record.fencing_token)
        if rollback.succeeded:
            rollback = backend.verify(
                item, original, fencing_token=record.fencing_token
            )
        if not rollback.succeeded:
            record.status = SystemOptimizationStatus.NEEDS_ATTENTION.value
            _set_problem(
                record,
                _problem(
                    "rollback",
                    "activation_rollback_failed",
                    "activation failed and the pre-activation value was not restored",
                    suggested_action="Block writes and recover the target out of band.",
                    apply=operation.model_dump(mode="json", exclude_none=False),
                    rollback=rollback.model_dump(mode="json", exclude_none=False),
                ),
            )
        else:
            activation.update(
                status="failed",
                phase="rolled-back",
                failure=operation.model_dump(mode="json", exclude_none=False),
            )
            record.activation_json = activation
            _set_problem(
                record,
                _problem(
                    "activation",
                    "activation_apply_failed",
                    "activation apply/readback failed; original value was restored",
                    suggested_action="Inspect executor evidence and run a new study if needed.",
                    operation=operation.model_dump(mode="json", exclude_none=False),
                ),
            )
        _touch(record)
        return record
    operation_digest = _put_artifact(
        session,
        cas,
        record,
        role="activation-operation",
        name="activation-readback.json",
        payload=operation.model_dump(mode="json", exclude_none=False),
    )
    activation.update(
        status="active",
        phase="active",
        operationDigest=operation_digest,
        activatedAt=utc_now().isoformat(),
    )
    record.activation_json = activation
    record.problem_json = None
    _touch(record)
    return record


def rollback_activated_optimization(
    session: Session,
    cas: FileSystemCAS,
    record: SystemOptimizationStudyRecord,
    *,
    expected_revision: int,
    backend: ExecutorBackend,
    manifest: ConfigManifest,
) -> SystemOptimizationStudyRecord:
    _assert_writes_allowed(record)
    _assert_revision(record, expected_revision)
    activation = dict(record.activation_json)
    if activation.get("phase") not in {"active", "snapshot-ready"}:
        _raise_problem(
            _problem(
                "rollback",
                "activation_rollback_transition_invalid",
                "there is no active or prepared runtime configuration to roll back",
                suggested_action="Reload the study and inspect activation status.",
                phase=activation.get("phase"),
            )
        )
    snapshot_digest = activation.get("snapshotDigest")
    if not isinstance(snapshot_digest, str):
        _raise_problem(
            _problem(
                "rollback",
                "activation_snapshot_missing",
                "explicit rollback has no pre-activation snapshot digest",
                suggested_action="Recover the target out of band.",
            )
        )
    hypothesis = _load_hypothesis(record, cas)
    item, _value = _candidate(hypothesis, manifest)
    snapshot = _read_json(cas, snapshot_digest)
    original = _snapshot_item_value(snapshot, item.id)
    if activation.get("rollbackPhase") != "fenced":
        record.fencing_token += 1
        activation["rollbackPhase"] = "fenced"
        record.activation_json = activation
        _touch(record)
        return record
    operation = backend.rollback(item, original, fencing_token=record.fencing_token)
    if operation.succeeded:
        operation = backend.verify(item, original, fencing_token=record.fencing_token)
    if not operation.succeeded:
        record.status = SystemOptimizationStatus.NEEDS_ATTENTION.value
        _set_problem(
            record,
            _problem(
                "rollback",
                "explicit_activation_rollback_failed",
                "explicit rollback did not restore and verify the pre-activation value",
                suggested_action="Block writes and recover the target out of band.",
                operation=operation.model_dump(mode="json", exclude_none=False),
            ),
        )
        _touch(record)
        return record
    operation_digest = _put_artifact(
        session,
        cas,
        record,
        role="activation-rollback",
        name="activation-rollback-readback.json",
        payload=operation.model_dump(mode="json", exclude_none=False),
    )
    activation.update(
        status="rolled-back",
        phase="rolled-back",
        rollbackPhase="completed",
        rollbackOperationDigest=operation_digest,
        rolledBackAt=utc_now().isoformat(),
    )
    record.activation_json = activation
    record.problem_json = None
    _touch(record)
    return record


def system_optimization_view(
    session: Session, record: SystemOptimizationStudyRecord
) -> dict[str, Any]:
    artifacts = list(
        session.scalars(
            select(SystemOptimizationArtifactLinkRecord)
            .where(SystemOptimizationArtifactLinkRecord.study_id == record.id)
            .order_by(SystemOptimizationArtifactLinkRecord.created_at)
        )
    )
    return {
        "id": record.id,
        "baselineCapacityStudyId": record.baseline_capacity_study_id,
        "candidateCapacityStudyId": record.candidate_capacity_study_id,
        "targetId": record.target_id,
        "network": record.network,
        "minimumEffect": record.minimum_effect,
        "authorizationProfileDigest": record.authorization_profile_digest,
        "status": record.status,
        "revision": record.revision,
        "hypothesisDigest": record.hypothesis_digest,
        "decisionDigest": record.decision_digest,
        "snapshotDigest": record.snapshot_digest,
        "rollbackVerified": record.rollback_verified,
        "orchestration": record.orchestration_json,
        "activation": record.activation_json,
        "problem": record.problem_json,
        "artifacts": [
            {
                "digest": item.digest,
                "role": item.role,
                "name": item.name,
                "mediaType": item.media_type,
            }
            for item in artifacts
        ],
        "createdAt": record.created_at,
        "updatedAt": record.updated_at,
        "approvedAt": record.approved_at,
        "completedAt": record.completed_at,
    }


class SimulatedCapacityDriver:
    def __init__(self, candidate_capacity_study_id: str) -> None:
        self.candidate_capacity_study_id = candidate_capacity_study_id
        self.submissions: dict[str, str] = {}
        self.observation = CapacityTaskObservation(
            capacity_study_id=candidate_capacity_study_id,
            status=CapacityTaskStatus.PENDING,
        )

    def submit_candidate(
        self,
        *,
        baseline_capacity_study_id: str,
        target_id: str,
        network: str,
        hypothesis_digest: str,
        idempotency_key: str,
    ) -> str:
        del baseline_capacity_study_id, target_id, network, hypothesis_digest
        self.submissions.setdefault(idempotency_key, self.candidate_capacity_study_id)
        return self.submissions[idempotency_key]

    def observe(self, capacity_study_id: str) -> CapacityTaskObservation:
        if capacity_study_id != self.candidate_capacity_study_id:
            raise KeyError(capacity_study_id)
        return self.observation

    def set_observation(self, observation: CapacityTaskObservation) -> None:
        if observation.capacity_study_id != self.candidate_capacity_study_id:
            raise ValueError("observation identity does not match the simulated candidate")
        self.observation = observation


class SimulatedStudyEvaluator:
    def __init__(self, result: StudyEvaluationResult) -> None:
        self.result = result
        self.calls = 0

    def evaluate(
        self,
        study: SystemOptimizationStudyRecord,
        observation: CapacityTaskObservation,
    ) -> StudyEvaluationResult:
        del study, observation
        self.calls += 1
        return self.result


__all__ = [
    "SYSTEM_OPTIMIZATION_ORCHESTRATION_SCHEMA",
    "CapacityStudyDriver",
    "CapacityTaskObservation",
    "CapacityTaskStatus",
    "ReconcileResult",
    "SimulatedCapacityDriver",
    "SimulatedStudyEvaluator",
    "StudyEvaluationResult",
    "StudyArtifactReference",
    "StudyEvaluator",
    "SystemOptimizationError",
    "SystemOptimizationProblem",
    "SystemOptimizationStatus",
    "approve_optimization_study",
    "apply_prepared_optimization_activation",
    "create_system_optimization_study",
    "halt_system_optimization_study",
    "link_system_optimization_artifact",
    "prepare_optimization_activation",
    "persist_system_optimization_artifact",
    "reconcile_system_optimization_study",
    "record_optimization_hypothesis",
    "report_system_optimization_problem",
    "recover_interrupted_system_optimization_studies",
    "request_optimization_approval",
    "rollback_activated_optimization",
    "system_optimization_view",
]
