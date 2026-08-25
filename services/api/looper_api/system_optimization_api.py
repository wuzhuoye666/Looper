from __future__ import annotations

import hmac
import json
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from looper_core.canonical import canonical_digest, canonical_json, utc_now
from looper_core.cas import FileSystemCAS, StoredArtifact
from looper_core.contracts import StrictModel
from looper_core.system_opt.config_manifest import (
    ConfigValueType,
    ValueDomain,
    ValueParser,
)
from looper_core.system_opt.demo import build_demo_manifest
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from looper_api.capacity_evidence import (
    CapacityEvidenceBundle,
    CapacityEvidenceError,
    build_capacity_study_evidence,
)
from looper_api.cloud_service import operator_auth_required, operator_token_ready
from looper_api.code_hypothesis import (
    HypothesisGenerationError,
    generate_code_driven_hypothesis,
)
from looper_api.config import Settings, get_settings
from looper_api.database import get_session
from looper_api.deepseek_credentials import effective_deepseek_settings
from looper_api.external_targets import ExternalTargetError, connect_existing_target
from looper_api.models import (
    ArtifactLinkRecord,
    ArtifactRecord,
    AttemptRecord,
    CapacityStudyRecord,
    SourceDiscoveryRecord,
    TargetRecord,
)
from looper_api.remote_credentials import RemoteCredentialError
from looper_api.remote_recovery import remembered_target_request
from looper_api.restricted_alibaba_sysfs import (
    RestrictedAlibabaBindingError,
    build_restricted_alibaba_sysfs_backend,
)
from looper_api.source_archive_store import EncryptedSourceArchiveStore, SourceArchiveError
from looper_api.system_optimization import (
    StudyArtifactReference,
    SystemOptimizationError,
    SystemOptimizationProblem,
    apply_prepared_optimization_activation,
    approve_optimization_study,
    create_system_optimization_study,
    link_system_optimization_artifact,
    persist_system_optimization_artifact,
    prepare_optimization_activation,
    record_optimization_hypothesis,
    report_system_optimization_problem,
    request_optimization_approval,
    rollback_activated_optimization,
    system_optimization_view,
)
from looper_api.system_optimization_inputs import (
    SystemOptimizationInputError,
    load_authorization_profile,
    load_runtime_diagnostic_profile_by_digest,
)
from looper_api.system_optimization_models import SystemOptimizationStudyRecord

router = APIRouter(prefix="/api/v1", tags=["system-optimization"])
operator_bearer = HTTPBearer(auto_error=False)
SessionDependency = Annotated[Session, Depends(get_session)]
SettingsDependency = Annotated[Settings, Depends(get_settings)]
OperatorCredentials = Annotated[
    HTTPAuthorizationCredentials | None, Depends(operator_bearer)
]


class SystemOptimizationCreateRequest(StrictModel):
    baseline_capacity_study_id: str = Field(
        alias="baselineCapacityStudyId", min_length=1, max_length=80
    )
    target_id: str = Field(alias="targetId", min_length=1, max_length=100)
    network: Literal["internal", "external"]
    minimum_effect: float = Field(alias="minimumEffect", ge=0)
    authorization_profile_digest: str = Field(
        alias="authorizationProfileDigest", pattern=r"^sha256:[0-9a-f]{64}$"
    )
    runtime_profile_digest: str = Field(
        alias="runtimeProfileDigest", pattern=r"^sha256:[0-9a-f]{64}$"
    )


class SystemOptimizationAuthorizationProfileRequest(StrictModel):
    target_id: str = Field(alias="targetId", min_length=1, max_length=100)
    runtime_profile_digest: str = Field(
        alias="runtimeProfileDigest", pattern=r"^sha256:[0-9a-f]{64}$"
    )


@router.get("/system-optimization-baseline-context")
def get_system_optimization_baseline_context_api(
    session: SessionDependency,
    settings: SettingsDependency,
    credentials: OperatorCredentials,
    baseline_capacity_study_id: str = Query(alias="baselineCapacityStudyId", min_length=1),
    target_id: str = Query(alias="targetId", min_length=1),
    network: Literal["internal", "external"] = Query(),
) -> dict[str, Any]:
    require_system_optimization_operator(credentials, settings)
    baseline = session.get(CapacityStudyRecord, baseline_capacity_study_id)
    target = session.get(TargetRecord, target_id)
    if baseline is None or target is None:
        raise HTTPException(status_code=404, detail="baseline study or target not found")
    try:
        bundle = build_capacity_study_evidence(
            session, baseline, _cas(settings), target_id=target_id, network=network
        )
    except CapacityEvidenceError as error:
        raise HTTPException(status_code=422, detail=error.issue.model_dump(mode="json")) from error
    return {
        "baselineCapacityStudyId": baseline.id,
        "targetId": target_id,
        "network": network,
        "experimentId": bundle.evidence.experiment_id,
        "contextDigest": bundle.evidence.context_digest,
        "frontier": bundle.evidence.frontier.model_dump(mode="json"),
    }


@router.get("/system-optimization-runtime-profiles/{experiment_id}")
def get_system_optimization_runtime_profile_api(
    experiment_id: str,
    session: SessionDependency,
    settings: SettingsDependency,
    credentials: OperatorCredentials,
) -> dict[str, Any]:
    require_system_optimization_operator(credentials, settings)
    link = session.scalar(
        select(ArtifactLinkRecord)
        .join(AttemptRecord, AttemptRecord.id == ArtifactLinkRecord.attempt_id)
        .where(
            AttemptRecord.experiment_id == experiment_id,
            AttemptRecord.status == "succeeded",
            ArtifactLinkRecord.role == "profile",
        )
        .order_by(ArtifactLinkRecord.created_at.desc())
    )
    if link is None:
        raise HTTPException(status_code=404, detail="runtime profile artifact is not ready")
    return {"experimentId": experiment_id, "digest": link.digest, "name": link.name}


class SystemOptimizationApproveRequest(StrictModel):
    hypothesis_digest: str = Field(
        alias="hypothesisDigest", pattern=r"^sha256:[0-9a-f]{64}$"
    )
    expected_revision: int = Field(alias="expectedRevision", ge=1)


class SystemOptimizationActivateRequest(StrictModel):
    decision_digest: str = Field(
        alias="decisionDigest", pattern=r"^sha256:[0-9a-f]{64}$"
    )
    expected_revision: int = Field(alias="expectedRevision", ge=1)
    authorization_profile_digest: str = Field(
        alias="authorizationProfileDigest", pattern=r"^sha256:[0-9a-f]{64}$"
    )


class SystemOptimizationRollbackRequest(StrictModel):
    expected_revision: int = Field(alias="expectedRevision", ge=1)


def require_system_optimization_operator(
    credentials: OperatorCredentials,
    settings: SettingsDependency,
) -> str:
    if not operator_auth_required(settings):
        return "local-readonly"
    if not operator_token_ready(settings):
        raise HTTPException(
            status_code=503,
            detail={
                "code": "operator_auth_not_configured",
                "message": "operator authentication is not configured",
            },
        )
    valid = bool(
        credentials
        and credentials.scheme.casefold() == "bearer"
        and hmac.compare_digest(credentials.credentials, settings.operator_token)
    )
    if not valid:
        raise HTTPException(
            status_code=401,
            detail={
                "code": "operator_auth_required",
                "message": "valid operator bearer token required",
            },
        )
    return "operator"


OperatorDependency = Annotated[str, Depends(require_system_optimization_operator)]


def _cas(settings: Settings) -> FileSystemCAS:
    return FileSystemCAS(settings.artifact_dir, max_bytes=settings.max_artifact_bytes)


def _http_problem(error: SystemOptimizationError, status_code: int = 409) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail=error.problem.model_dump(mode="json", exclude_none=False),
    )


def _target_access_problem(error: Exception) -> HTTPException:
    code = (
        error.code
        if isinstance(error, SystemOptimizationInputError)
        else "target_revalidation_failed"
    )
    problem = SystemOptimizationProblem(
        stage="activation",
        code=code,
        message="target authorization or pinned SSH identity could not be revalidated",
        evidence_summary={"errorType": type(error).__name__},
        suggested_action=(
            "Restore the verified authorization artifact and pinned saved credentials, "
            "then retry with the current revision."
        ),
    )
    return HTTPException(
        status_code=409,
        detail=problem.model_dump(mode="json", exclude_none=False),
    )


def _stored_reference(
    role: str, name: str, artifact: StoredArtifact
) -> StudyArtifactReference:
    return StudyArtifactReference(
        digest=artifact.digest,
        size=artifact.size,
        role=role,
        name=name,
    )


def _capacity_references(
    prefix: str, bundle: CapacityEvidenceBundle
) -> list[StudyArtifactReference]:
    return [
        _stored_reference(
            "capacity-report", f"{prefix}-capacity-report.json", bundle.report_artifact
        ),
        _stored_reference(
            "capacity-contract",
            f"{prefix}-capacity-study-contract.json",
            bundle.study_contract_artifact,
        ),
        _stored_reference(
            "capacity-contract",
            f"{prefix}-capacity-experiment-contract.json",
            bundle.experiment_contract_artifact,
        ),
        _stored_reference(
            "capacity-evidence",
            f"{prefix}-normalized-capacity-evidence.json",
            bundle.normalized_artifact,
        ),
        _stored_reference(
            "capacity-manifest",
            f"{prefix}-capacity-evidence-manifest.json",
            bundle.manifest_artifact,
        ),
    ]


def _link_registered_artifact(
    session: Session,
    cas: FileSystemCAS,
    record: SystemOptimizationStudyRecord,
    *,
    digest: str,
    role: str,
    name: str,
) -> None:
    artifact = session.get(ArtifactRecord, digest)
    if artifact is None:
        raise SystemOptimizationInputError(
            f"{role}_missing", "required artifact is not registered", recoverable=True
        )
    link_system_optimization_artifact(
        session,
        cas,
        record,
        StudyArtifactReference(
            digest=digest,
            size=artifact.size,
            role=role,
            name=name,
        ),
    )


def _runtime_authorization_payload(
    profile: Any, *, target_id: str
) -> dict[str, Any]:
    summary = profile.measurement_summary
    device = str(summary.get("device") or "")
    scheduler_choices = json.loads(summary.get("schedulerChoices") or "[]")
    nomerges_choices = json.loads(summary.get("nomergesChoices") or "[]")
    scheduler_active = str(summary.get("schedulerActive") or "")
    nomerges_active = int(summary.get("nomergesActive") or 0)
    if not device or not scheduler_choices or scheduler_active not in scheduler_choices:
        raise SystemOptimizationInputError(
            "runtime_profile_invalid",
            "runtime profile does not contain a verified scheduler domain",
        )
    if not nomerges_choices or nomerges_active not in nomerges_choices:
        raise SystemOptimizationInputError(
            "runtime_profile_invalid",
            "runtime profile does not contain a verified nomerges domain",
        )
    demo = build_demo_manifest()
    scheduler = demo.item("storage-scheduler").model_copy(
        update={
            "target": f"/sys/block/{device}/queue/scheduler",
            "domain": ValueDomain(
                minimum=None,
                maximum=None,
                step=None,
                choices=scheduler_choices,
                log=False,
            ),
            "default": scheduler_active,
            "source": "target-bound runtime profile dynamic scheduler domain",
            "description": "Observed Alibaba ECS block-device scheduler domain.",
        }
    )
    scheduler = scheduler.model_copy(
        update={
            "compatibility": scheduler.compatibility.model_copy(
                update={"required_paths": [scheduler.target]}
            )
        }
    )
    nomerges = scheduler.model_copy(
        update={
            "id": "storage-nomerges",
            "target": f"/sys/block/{device}/queue/nomerges",
            "value_type": ConfigValueType.INTEGER,
            "domain": ValueDomain(
                minimum=min(int(value) for value in nomerges_choices),
                maximum=max(int(value) for value in nomerges_choices),
                step=1,
                choices=None,
                log=False,
            ),
            "default": nomerges_active,
            "read": scheduler.read.model_copy(update={"parser": ValueParser.INTEGER}),
            "source": "target-bound runtime profile dynamic nomerges domain",
            "description": "Observed Alibaba ECS block-device merge policy domain.",
            "compatibility": scheduler.compatibility.model_copy(
                update={"required_paths": [f"/sys/block/{device}/queue/nomerges"]}
            ),
        }
    )
    manifest = demo.model_copy(
        update={
            "id": "alibaba-ecs-storage-queue",
            "version": "1",
            "description": (
                "Target-bound Alibaba ECS storage queue controls from read-only runtime evidence."
            ),
            "items": [scheduler, nomerges],
            "metadata": {
                "evidenceKind": "target-bound-runtime-profile",
                "targetId": target_id,
                "device": device,
            },
        }
    )
    resolved = []
    for item in manifest.items:
        domain = item.domain
        resolved.append(
            {
                "item_id": item.id,
                "parameter_id": item.parameter_id,
                "value_type": item.value_type.value,
                "minimum": domain.minimum,
                "maximum": domain.maximum,
                "step": domain.step,
                "choices": domain.choices,
                "log": domain.log,
                "sources": [item.source, "runtime profile artifact"],
            }
        )
    return {
        "schemaVersion": "looper.system-optimization-authorization/v1alpha1",
        "targetId": target_id,
        "manifest": manifest.model_dump(mode="json", by_alias=True),
        "resolvedDomains": resolved,
        "reason": "User-requested target-bound study; activation requires separate approval.",
    }


@router.post("/system-optimization-authorization-profiles", status_code=201)
def create_system_optimization_authorization_profile_api(
    request: SystemOptimizationAuthorizationProfileRequest,
    session: SessionDependency,
    settings: SettingsDependency,
    credentials: OperatorCredentials,
) -> dict[str, Any]:
    require_system_optimization_operator(credentials, settings)
    target = session.get(TargetRecord, request.target_id)
    if target is None or target.provider != "alibaba" or target.lifecycle_status != "active":
        raise HTTPException(status_code=422, detail="an active Alibaba target is required")
    cas = _cas(settings)
    try:
        profile = load_runtime_diagnostic_profile_by_digest(
            session, cas, request.runtime_profile_digest, target_id=request.target_id
        )
        payload = _runtime_authorization_payload(profile, target_id=request.target_id)
        stored = cas.put_bytes(canonical_json(payload).encode("utf-8"))
        if session.get(ArtifactRecord, stored.digest) is None:
            session.add(
                ArtifactRecord(
                    digest=stored.digest,
                    size=stored.size,
                    verified=True,
                    created_at=utc_now(),
                )
            )
        session.commit()
    except SystemOptimizationInputError as error:
        raise HTTPException(
            status_code=422,
            detail={"code": error.code, "message": str(error)},
        ) from error
    return {
        "digest": stored.digest,
        "targetId": request.target_id,
        "runtimeProfileDigest": request.runtime_profile_digest,
        "manifestDigest": canonical_digest(payload["manifest"]),
        "activation": "approval-required",
    }


def _record_input_problem(
    record: SystemOptimizationStudyRecord,
    *,
    code: str,
    message: str,
    recoverable: bool,
) -> None:
    report_system_optimization_problem(
        record,
        SystemOptimizationProblem(
            stage="hypothesis",
            code=code,
            message=message,
            evidence_summary={"recoverable": recoverable},
            suggested_action=(
                "Attach the missing digest-bound evidence and create a new study."
                if recoverable
                else "Inspect the referenced evidence contract before retrying."
            ),
        ),
    )


@router.post("/system-optimization-studies", status_code=201)
async def create_system_optimization_study_api(
    request: SystemOptimizationCreateRequest,
    session: SessionDependency,
    settings: SettingsDependency,
    _operator: OperatorDependency,
) -> dict[str, Any]:
    baseline = session.get(CapacityStudyRecord, request.baseline_capacity_study_id)
    target = session.get(TargetRecord, request.target_id)
    if baseline is None:
        raise HTTPException(status_code=404, detail="baseline capacity study not found")
    if target is None:
        raise HTTPException(status_code=404, detail="target not found")
    if target.provider != "alibaba" or target.lifecycle_status != "active":
        raise HTTPException(
            status_code=422,
            detail="v1 requires an active Alibaba Cloud ECS target",
        )
    cas = _cas(settings)
    try:
        capacity = build_capacity_study_evidence(
            session,
            baseline,
            cas,
            target_id=request.target_id,
            network=request.network,
        )
    except CapacityEvidenceError as error:
        raise HTTPException(
            status_code=422,
            detail=error.issue.model_dump(mode="json", exclude_none=False),
        ) from error
    record = create_system_optimization_study(
        session,
        baseline_capacity_study_id=baseline.id,
        target_id=target.id,
        network=request.network,
        minimum_effect=request.minimum_effect,
        authorization_profile_digest=request.authorization_profile_digest,
    )
    for reference in _capacity_references("baseline", capacity):
        link_system_optimization_artifact(session, cas, record, reference)
    try:
        authorization = load_authorization_profile(
            session,
            cas,
            request.authorization_profile_digest,
            target_id=target.id,
        )
        runtime_digest = request.runtime_profile_digest
        runtime = load_runtime_diagnostic_profile_by_digest(
            session, cas, runtime_digest, target_id=target.id
        )
        if runtime.capacity_context_digest != capacity.evidence.context_digest:
            raise SystemOptimizationInputError(
                "runtime_profile_context_mismatch",
                "selected runtime profile is bound to a different capacity context",
                recoverable=True,
            )
        _link_registered_artifact(
            session,
            cas,
            record,
            digest=request.authorization_profile_digest,
            role="authorization-profile",
            name="authorization-profile.json",
        )
        _link_registered_artifact(
            session,
            cas,
            record,
            digest=runtime_digest,
            role="runtime-profile",
            name="runtime-diagnostic-profile.json",
        )
        discovery = session.get(SourceDiscoveryRecord, baseline.discovery_id)
        if discovery is None:
            raise SystemOptimizationInputError(
                "source_archive_missing", "baseline source discovery is missing"
            )
        source_archive = EncryptedSourceArchiveStore(settings).load(discovery.id)
        generation = await generate_code_driven_hypothesis(
            capacity=capacity.evidence,
            source_archive=source_archive,
            runtime_profile_digest=runtime_digest,
            priorities=runtime.priorities,
            manifest=authorization.manifest,
            resolved_domains=authorization.domain_mapping(),
            settings=effective_deepseek_settings(settings),
        )
        contract_digest = persist_system_optimization_artifact(
            session,
            cas,
            record,
            role="configuration-contract",
            name="authorized-configuration-contract.json",
            payload=generation.configuration_contract,
        )
        if contract_digest != generation.configuration_contract_digest:
            raise SystemOptimizationInputError(
                "configuration_contract_digest_mismatch",
                "generated configuration contract changed during persistence",
            )
        persist_system_optimization_artifact(
            session,
            cas,
            record,
            role="hypothesis-generation",
            name="code-driven-hypothesis-result.json",
            payload=generation.model_dump(mode="json", exclude_none=False),
        )
        record_optimization_hypothesis(
            session,
            cas,
            record,
            generation.hypothesis,
            expected_revision=record.revision,
        )
        request_optimization_approval(record, expected_revision=record.revision)
    except SystemOptimizationInputError as error:
        _record_input_problem(
            record,
            code=error.code,
            message=str(error),
            recoverable=error.recoverable,
        )
    except HypothesisGenerationError as error:
        _record_input_problem(
            record,
            code=error.issue.code,
            message=error.issue.message,
            recoverable=error.issue.recoverable,
        )
    except SourceArchiveError as error:
        _record_input_problem(
            record,
            code="source_archive_missing",
            message=str(error),
            recoverable=True,
        )
    session.commit()
    return system_optimization_view(session, record)


@router.get("/system-optimization-studies/{study_id}")
def get_system_optimization_study_api(
    study_id: str,
    session: SessionDependency,
    _operator: OperatorDependency,
) -> dict[str, Any]:
    record = session.get(SystemOptimizationStudyRecord, study_id)
    if record is None:
        raise HTTPException(status_code=404, detail="system optimization study not found")
    return system_optimization_view(session, record)


@router.post("/system-optimization-studies/{study_id}/approve")
def approve_system_optimization_study_api(
    study_id: str,
    request: SystemOptimizationApproveRequest,
    session: SessionDependency,
    _operator: OperatorDependency,
) -> dict[str, Any]:
    record = session.get(SystemOptimizationStudyRecord, study_id)
    if record is None:
        raise HTTPException(status_code=404, detail="system optimization study not found")
    try:
        approve_optimization_study(
            record,
            hypothesis_digest=request.hypothesis_digest,
            expected_revision=request.expected_revision,
        )
        session.commit()
    except SystemOptimizationError as error:
        session.rollback()
        raise _http_problem(error) from error
    return system_optimization_view(session, record)


@router.post("/system-optimization-studies/{study_id}/activate")
def activate_system_optimization_study_api(
    study_id: str,
    request: SystemOptimizationActivateRequest,
    session: SessionDependency,
    settings: SettingsDependency,
    _operator: OperatorDependency,
) -> dict[str, Any]:
    record = session.get(SystemOptimizationStudyRecord, study_id)
    if record is None:
        raise HTTPException(status_code=404, detail="system optimization study not found")
    if request.authorization_profile_digest != record.authorization_profile_digest:
        raise HTTPException(status_code=409, detail="authorization profile digest changed")
    cas = _cas(settings)
    try:
        authorization = load_authorization_profile(
            session,
            cas,
            request.authorization_profile_digest,
            target_id=record.target_id,
        )
        target = session.get(TargetRecord, record.target_id)
        if target is None:
            raise HTTPException(status_code=404, detail="target not found")
        refreshed = connect_existing_target(
            session,
            target,
            remembered_target_request(target, settings),
        )
        session.commit()
        backend = build_restricted_alibaba_sysfs_backend(
            session, record.target_id, settings
        )
        prepare_optimization_activation(
            session,
            cas,
            record,
            decision_digest=request.decision_digest,
            expected_revision=request.expected_revision,
            current_environment_digest=refreshed.snapshot_digest,
            current_authorization_profile_digest=request.authorization_profile_digest,
            backend=backend,
            manifest=authorization.manifest,
        )
        session.commit()
        prepare_optimization_activation(
            session,
            cas,
            record,
            decision_digest=request.decision_digest,
            expected_revision=record.revision,
            current_environment_digest=refreshed.snapshot_digest,
            current_authorization_profile_digest=request.authorization_profile_digest,
            backend=backend,
            manifest=authorization.manifest,
        )
        session.commit()
        apply_prepared_optimization_activation(
            session,
            cas,
            record,
            expected_revision=record.revision,
            current_environment_digest=refreshed.snapshot_digest,
            current_authorization_profile_digest=request.authorization_profile_digest,
            backend=backend,
            manifest=authorization.manifest,
        )
        session.commit()
    except (
        SystemOptimizationInputError,
        ExternalTargetError,
        RemoteCredentialError,
        RestrictedAlibabaBindingError,
    ) as error:
        session.rollback()
        raise _target_access_problem(error) from error
    except SystemOptimizationError as error:
        session.rollback()
        raise _http_problem(error) from error
    return system_optimization_view(session, record)


@router.post("/system-optimization-studies/{study_id}/rollback")
def rollback_system_optimization_study_api(
    study_id: str,
    request: SystemOptimizationRollbackRequest,
    session: SessionDependency,
    settings: SettingsDependency,
    _operator: OperatorDependency,
) -> dict[str, Any]:
    record = session.get(SystemOptimizationStudyRecord, study_id)
    if record is None:
        raise HTTPException(status_code=404, detail="system optimization study not found")
    cas = _cas(settings)
    try:
        authorization = load_authorization_profile(
            session,
            cas,
            record.authorization_profile_digest,
            target_id=record.target_id,
        )
        backend = build_restricted_alibaba_sysfs_backend(
            session, record.target_id, settings
        )
        rollback_activated_optimization(
            session,
            cas,
            record,
            expected_revision=request.expected_revision,
            backend=backend,
            manifest=authorization.manifest,
        )
        session.commit()
        rollback_activated_optimization(
            session,
            cas,
            record,
            expected_revision=record.revision,
            backend=backend,
            manifest=authorization.manifest,
        )
        session.commit()
    except (
        SystemOptimizationInputError,
        RemoteCredentialError,
        RestrictedAlibabaBindingError,
    ) as error:
        session.rollback()
        raise _target_access_problem(error) from error
    except SystemOptimizationError as error:
        session.rollback()
        raise _http_problem(error) from error
    return system_optimization_view(session, record)


__all__ = [
    "SystemOptimizationActivateRequest",
    "SystemOptimizationApproveRequest",
    "SystemOptimizationCreateRequest",
    "SystemOptimizationRollbackRequest",
    "router",
]
