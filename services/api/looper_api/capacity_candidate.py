from __future__ import annotations

import copy
import hashlib
from collections.abc import Callable
from typing import Any, Literal

from looper_core.canonical import canonical_digest, utc_now
from looper_core.cas import FileSystemCAS, StoredArtifact
from looper_core.contracts import StrictModel
from looper_core.system_opt.hypothesis import (
    CapacityDecisionStatus,
    evaluate_capacity_frontiers,
)
from pydantic import Field
from sqlalchemy.orm import Session

from looper_api.capacity import (
    CapacityDraft,
    CapacityStartRequest,
    preflight_capacity_study,
    start_capacity_study,
)
from looper_api.capacity_evidence import (
    CapacityEvidenceBundle,
    CapacityStudyEvidence,
    build_capacity_study_evidence,
)
from looper_api.config import Settings
from looper_api.models import CapacityStudyRecord, SourceDiscoveryRecord
from looper_api.system_optimization import (
    CapacityTaskObservation,
    CapacityTaskStatus,
    StudyArtifactReference,
    StudyEvaluationResult,
)
from looper_api.system_optimization_models import SystemOptimizationStudyRecord

CANDIDATE_CLONE_SCHEMA = "looper.capacity-candidate-clone/v1alpha1"
CONTROL_DRIFT_FORMULA_ID = "F-CONTROL-FRONTIER-OVERLAP-001/v1alpha1"


class CapacityCandidateClone(StrictModel):
    schema_version: Literal[CANDIDATE_CLONE_SCHEMA] = CANDIDATE_CLONE_SCHEMA
    baseline_capacity_study_id: str
    candidate_capacity_study_id: str
    idempotency_key: str
    source_digest: str
    draft_digest: str
    build_digest: str
    scenario_digest: str
    slo_digest: str
    targets_digest: str
    budget_digest: str
    active_target_ids: list[str] = Field(min_length=1)
    tuned_target_id: str
    network: Literal["internal", "external"]

    @property
    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json", exclude_none=False))


def _candidate_id(idempotency_key: str) -> str:
    suffix = hashlib.sha256(idempotency_key.encode()).hexdigest()[:40]
    return f"capacity_opt_{suffix}"


def build_capacity_candidate_clone(
    session: Session,
    baseline: CapacityStudyRecord,
    *,
    candidate_capacity_study_id: str,
    target_id: str,
    network: Literal["internal", "external"],
    idempotency_key: str,
) -> tuple[CapacityDraft, CapacityCandidateClone]:
    if baseline.status != "completed" or baseline.report_json is None:
        raise ValueError("baseline capacity study must be completed with a report")
    discovery = session.get(SourceDiscoveryRecord, baseline.discovery_id)
    if discovery is None or discovery.status != "completed":
        raise ValueError("baseline source discovery is unavailable or incomplete")
    active_target_ids = list(baseline.execution_json.get("activeTargetIds") or [])
    if not active_target_ids or len(active_target_ids) != len(set(active_target_ids)):
        raise ValueError("baseline active target identity is missing or ambiguous")
    if target_id not in active_target_ids:
        raise ValueError("tuned target is not part of the baseline capacity run")
    baseline_draft = CapacityDraft.model_validate(baseline.draft_json)
    payload = baseline_draft.model_dump(mode="json", by_alias=True)
    targets = dict(payload["targets"])
    targets["sutIds"] = list(active_target_ids)
    targets["internalBaseUrls"] = {
        target: value
        for target, value in targets["internalBaseUrls"].items()
        if target in active_target_ids
    }
    targets["externalBaseUrls"] = {
        target: value
        for target, value in targets["externalBaseUrls"].items()
        if target in active_target_ids
    }
    payload["targets"] = targets
    candidate_draft = CapacityDraft.model_validate(payload)
    candidate_payload = candidate_draft.model_dump(mode="json", by_alias=True)
    clone = CapacityCandidateClone(
        baseline_capacity_study_id=baseline.id,
        candidate_capacity_study_id=candidate_capacity_study_id,
        idempotency_key=idempotency_key,
        source_digest=discovery.source_digest,
        draft_digest=canonical_digest(candidate_payload),
        build_digest=canonical_digest(candidate_payload["build"]),
        scenario_digest=canonical_digest(candidate_payload["scenario"]),
        slo_digest=canonical_digest(candidate_payload["slo"]),
        targets_digest=canonical_digest(candidate_payload["targets"]),
        budget_digest=canonical_digest(candidate_payload["budget"]),
        active_target_ids=active_target_ids,
        tuned_target_id=target_id,
        network=network,
    )
    return candidate_draft, clone


class CapacityRecordStudyDriver:
    def __init__(
        self,
        session_factory: Callable[[], Session],
        settings: Settings,
        *,
        preflight: Callable[[Session, CapacityStudyRecord, Settings], dict[str, Any]] = (
            preflight_capacity_study
        ),
        start: Callable[
            [Session, CapacityStudyRecord, CapacityStartRequest, Settings],
            CapacityStudyRecord,
        ] = start_capacity_study,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.preflight = preflight
        self.start = start

    def submit_candidate(
        self,
        *,
        baseline_capacity_study_id: str,
        target_id: str,
        network: str,
        hypothesis_digest: str,
        idempotency_key: str,
    ) -> str:
        if network not in {"internal", "external"}:
            raise ValueError("candidate capacity network is invalid")
        candidate_id = _candidate_id(idempotency_key)
        with self.session_factory() as session:
            existing = session.get(CapacityStudyRecord, candidate_id)
            if existing is not None:
                clone = (existing.execution_json or {}).get("optimizationClone") or {}
                if (
                    clone.get("idempotency_key") != idempotency_key
                    or clone.get("baseline_capacity_study_id")
                    != baseline_capacity_study_id
                    or clone.get("tuned_target_id") != target_id
                ):
                    raise RuntimeError("candidate capacity idempotency identity conflict")
                return existing.id
            baseline = session.get(CapacityStudyRecord, baseline_capacity_study_id)
            if baseline is None:
                raise ValueError("baseline capacity study does not exist")
            draft, clone = build_capacity_candidate_clone(
                session,
                baseline,
                candidate_capacity_study_id=candidate_id,
                target_id=target_id,
                network=network,  # type: ignore[arg-type]
                idempotency_key=idempotency_key,
            )
            now = utc_now()
            record = CapacityStudyRecord(
                id=candidate_id,
                discovery_id=baseline.discovery_id,
                name=f"{baseline.name} · optimization candidate"[:160],
                status="draft",
                revision=1,
                current_step=baseline.current_step,
                draft_json=draft.model_dump(mode="json", by_alias=True),
                preflight_json={},
                execution_json={
                    "phases": [],
                    "runs": [],
                    "buildValidations": copy.deepcopy(
                        baseline.execution_json.get("buildValidations") or []
                    ),
                    "optimizationClone": clone.model_dump(
                        mode="json", exclude_none=False
                    ),
                    "optimizationCloneDigest": clone.digest,
                    "hypothesisDigest": hypothesis_digest,
                },
                report_json=None,
                error_code=None,
                error_message=None,
                created_at=now,
                updated_at=now,
                started_at=None,
                completed_at=None,
            )
            session.add(record)
            session.flush()
            try:
                result = self.preflight(session, record, self.settings)
                failed = list(result.get("failedSutIds") or [])
                generator_failures = list(result.get("generatorFailures") or [])
                if failed or generator_failures:
                    raise ValueError(
                        "candidate preflight cannot exclude cloned servers or load generators"
                    )
                self.start(
                    session,
                    record,
                    CapacityStartRequest(
                        expectedRevision=record.revision,
                        excludedTargetIds=[],
                        acknowledgePartial=False,
                    ),
                    self.settings,
                )
                execution = dict(record.execution_json)
                execution.update(
                    {
                        "optimizationClone": clone.model_dump(
                            mode="json", exclude_none=False
                        ),
                        "optimizationCloneDigest": clone.digest,
                        "hypothesisDigest": hypothesis_digest,
                    }
                )
                record.execution_json = execution
            except Exception as error:
                record.status = "failed"
                record.error_code = "optimization_candidate_start_failed"
                record.error_message = str(error)[:16000]
                record.completed_at = utc_now()
                record.updated_at = record.completed_at
            session.commit()
            return record.id

    def observe(self, capacity_study_id: str) -> CapacityTaskObservation:
        with self.session_factory() as session:
            record = session.get(CapacityStudyRecord, capacity_study_id)
            if record is None:
                raise KeyError(capacity_study_id)
            if record.status == "completed" and record.report_json is not None:
                status = CapacityTaskStatus.COMPLETED
                report_digest = canonical_digest(record.report_json)
            elif record.status in {"failed", "needs-attention"}:
                status = CapacityTaskStatus.FAILED
                report_digest = None
            elif record.status == "cancelled":
                status = CapacityTaskStatus.CANCELLED
                report_digest = None
            else:
                status = CapacityTaskStatus.PENDING
                report_digest = None
            return CapacityTaskObservation(
                capacity_study_id=record.id,
                status=status,
                report_digest=report_digest,
                error_code=record.error_code,
                evidence={
                    "capacityStatus": record.status,
                    "optimizationCloneDigest": (
                        record.execution_json.get("optimizationCloneDigest")
                    ),
                },
            )


def _frontier_overlap(
    baseline: CapacityStudyEvidence, candidate: CapacityStudyEvidence
) -> tuple[list[str], dict[str, Any]]:
    baseline_ids = set(baseline.control_frontiers)
    candidate_ids = set(candidate.control_frontiers)
    details: dict[str, Any] = {
        "formulaId": CONTROL_DRIFT_FORMULA_ID,
        "baselineControlIds": sorted(baseline_ids),
        "candidateControlIds": sorted(candidate_ids),
        "controls": {},
    }
    if baseline_ids != candidate_ids:
        return ["control-target-set-changed"], details
    drifted: list[str] = []
    for target_id in sorted(baseline_ids):
        before = baseline.control_frontiers[target_id]
        after = candidate.control_frontiers[target_id]
        lower = max(before.confirmed_pass, after.confirmed_pass)
        upper = min(before.confirmed_fail, after.confirmed_fail)
        overlaps = lower < upper
        details["controls"][target_id] = {
            "baseline": before.model_dump(mode="json"),
            "candidate": after.model_dump(mode="json"),
            "positiveWidthOverlap": overlaps,
        }
        if not overlaps:
            drifted.append(target_id)
    return drifted, details


def evaluate_capacity_with_controls(
    *,
    hypothesis_digest: str,
    baseline: CapacityStudyEvidence,
    candidate: CapacityStudyEvidence,
    minimum_effect: float,
    rollback_verified: bool,
) -> StudyEvaluationResult:
    core = evaluate_capacity_frontiers(
        hypothesis_digest=hypothesis_digest,
        baseline_frontier=baseline.frontier.model_dump(mode="json"),
        candidate_frontier=candidate.frontier.model_dump(mode="json"),
        baseline_report_digest=baseline.report_digest,
        candidate_report_digest=candidate.report_digest,
        baseline_identity=baseline.identity,
        candidate_identity=candidate.identity,
        minimum_effect=minimum_effect,
        rollback_verified=rollback_verified,
    )
    drifted, controls = _frontier_overlap(baseline, candidate)
    decision: dict[str, Any] = {
        "schemaVersion": "looper.controlled-capacity-decision/v1alpha1",
        "coreDecision": core.model_dump(mode="json", exclude_none=False),
        "measurementIdentity": dict(baseline.identity),
        "controlDrift": controls,
        "rollbackVerified": rollback_verified,
    }
    if core.status == CapacityDecisionStatus.ACCEPTED and not baseline.control_frontiers:
        outcome = "inconclusive"
        decision.update(
            status="provisional",
            reason="no untuned control server is available; automatic activation is forbidden",
        )
    elif drifted:
        outcome = "inconclusive"
        decision.update(
            status="inconclusive",
            reason="untuned control servers show distinguishable capacity drift",
            driftedControlTargetIds=drifted,
        )
    elif core.status == CapacityDecisionStatus.ACCEPTED:
        outcome = "accepted"
        decision.update(status="accepted", reason=core.reason)
    elif core.status == CapacityDecisionStatus.REJECTED:
        outcome = "rejected"
        decision.update(status="rejected", reason=core.reason)
    elif core.status in {
        CapacityDecisionStatus.INCOMPARABLE,
        CapacityDecisionStatus.SAFETY_FAILED,
    }:
        outcome = "blocked"
        decision.update(status="blocked", reason=core.reason)
    else:
        outcome = "inconclusive"
        decision.update(status="inconclusive", reason=core.reason)
    return StudyEvaluationResult(outcome=outcome, decision=decision)


def _references(prefix: str, bundle: CapacityEvidenceBundle) -> list[StudyArtifactReference]:
    values: list[tuple[str, str, StoredArtifact]] = [
        ("capacity-report", f"{prefix}-capacity-report.json", bundle.report_artifact),
        ("capacity-contract", f"{prefix}-study-contract.json", bundle.study_contract_artifact),
        (
            "capacity-contract",
            f"{prefix}-experiment-contract.json",
            bundle.experiment_contract_artifact,
        ),
        ("capacity-evidence", f"{prefix}-normalized-evidence.json", bundle.normalized_artifact),
        ("capacity-manifest", f"{prefix}-evidence-manifest.json", bundle.manifest_artifact),
    ]
    return [
        StudyArtifactReference(digest=value.digest, size=value.size, role=role, name=name)
        for role, name, value in values
    ]


class RealCapacityStudyEvaluator:
    def __init__(
        self,
        session_factory: Callable[[], Session],
        cas: FileSystemCAS,
    ) -> None:
        self.session_factory = session_factory
        self.cas = cas

    def evaluate(
        self,
        study: SystemOptimizationStudyRecord,
        observation: CapacityTaskObservation,
    ) -> StudyEvaluationResult:
        if observation.capacity_study_id != study.candidate_capacity_study_id:
            raise ValueError("candidate capacity observation identity mismatch")
        if study.hypothesis_digest is None:
            raise ValueError("optimization study hypothesis digest is missing")
        with self.session_factory() as session:
            baseline_record = session.get(
                CapacityStudyRecord, study.baseline_capacity_study_id
            )
            candidate_record = session.get(
                CapacityStudyRecord, study.candidate_capacity_study_id
            )
            if baseline_record is None or candidate_record is None:
                raise ValueError("baseline or candidate capacity study is missing")
            baseline = build_capacity_study_evidence(
                session,
                baseline_record,
                self.cas,
                target_id=study.target_id,
                network=study.network,
            )
            candidate = build_capacity_study_evidence(
                session,
                candidate_record,
                self.cas,
                target_id=study.target_id,
                network=study.network,
            )
        result = evaluate_capacity_with_controls(
            hypothesis_digest=study.hypothesis_digest,
            baseline=baseline.evidence,
            candidate=candidate.evidence,
            minimum_effect=study.minimum_effect,
            rollback_verified=study.rollback_verified,
        )
        return result.model_copy(
            update={
                "artifacts": [
                    *_references("baseline", baseline),
                    *_references("candidate", candidate),
                ]
            }
        )


__all__ = [
    "CANDIDATE_CLONE_SCHEMA",
    "CONTROL_DRIFT_FORMULA_ID",
    "CapacityCandidateClone",
    "CapacityRecordStudyDriver",
    "RealCapacityStudyEvaluator",
    "build_capacity_candidate_clone",
    "evaluate_capacity_with_controls",
]
