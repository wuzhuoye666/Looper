from __future__ import annotations

import json
from typing import Literal

from looper_core.cas import ArtifactError, FileSystemCAS
from looper_core.contracts import StrictModel
from looper_core.state import AttemptStatus
from looper_core.system_opt.config_manifest import ConfigManifest
from looper_core.system_opt.domain import ResolvedDomain
from looper_core.system_opt.scoring import DiagnosticPriority
from pydantic import Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from looper_api.models import (
    ArtifactLinkRecord,
    ArtifactRecord,
    AttemptRecord,
)

AUTHORIZATION_PROFILE_SCHEMA = "looper.system-optimization-authorization/v1alpha1"
RUNTIME_PROFILE_SCHEMA = "looper.runtime-diagnostic-profile/v1alpha1"


class SystemOptimizationInputError(RuntimeError):
    def __init__(self, code: str, message: str, *, recoverable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.recoverable = recoverable


class SystemOptimizationAuthorizationProfile(StrictModel):
    schema_version: Literal[AUTHORIZATION_PROFILE_SCHEMA] = Field(alias="schemaVersion")
    target_id: str = Field(alias="targetId", min_length=1, max_length=100)
    manifest: ConfigManifest
    resolved_domains: list[ResolvedDomain] = Field(
        alias="resolvedDomains", min_length=1
    )
    reason: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def validate_domains(self) -> SystemOptimizationAuthorizationProfile:
        mapping = {item.parameter_id: item for item in self.resolved_domains}
        if len(mapping) != len(self.resolved_domains):
            raise ValueError("resolved domain parameter ids must be unique")
        for parameter_id, domain in mapping.items():
            item = self.manifest.item_for_parameter(parameter_id)
            if domain.item_id != item.id:
                raise ValueError("resolved domain item identity does not match the manifest")
            if domain.value_type != item.value_type:
                raise ValueError("resolved domain value type does not match the manifest")
            if domain.choices is not None:
                for value in domain.choices:
                    item.validate_value(value)
            elif domain.minimum is not None and domain.maximum is not None:
                declared = item.domain
                if (
                    declared.minimum is None
                    or declared.maximum is None
                    or domain.minimum < declared.minimum
                    or domain.maximum > declared.maximum
                    or domain.step != declared.step
                ):
                    raise ValueError("resolved numeric domain exceeds the manifest domain")
        return self

    def domain_mapping(self) -> dict[str, ResolvedDomain]:
        return {item.parameter_id: item for item in self.resolved_domains}


class RuntimeDiagnosticProfile(StrictModel):
    schema_version: Literal[RUNTIME_PROFILE_SCHEMA] = Field(alias="schemaVersion")
    capacity_context_digest: str = Field(
        alias="capacityContextDigest", pattern=r"^sha256:[0-9a-f]{64}$"
    )
    target_id: str = Field(alias="targetId", min_length=1, max_length=100)
    priorities: list[DiagnosticPriority] = Field(min_length=1)
    measurement_summary: dict[str, str] = Field(
        default_factory=dict, alias="measurementSummary"
    )


def _load_json_artifact(
    session: Session,
    cas: FileSystemCAS,
    digest: str,
    *,
    missing_code: str,
) -> dict[str, object]:
    artifact = session.get(ArtifactRecord, digest)
    if artifact is None or not artifact.verified:
        raise SystemOptimizationInputError(
            missing_code,
            "required verified artifact is not registered",
            recoverable=True,
        )
    try:
        stored = cas.verify(digest, expected_size=artifact.size)
        if stored.size > 4 * 1024 * 1024:
            raise ValueError("artifact exceeds the input contract size limit")
        with stored.path.open("r", encoding="utf-8") as source:
            payload = json.load(source)
    except (ArtifactError, OSError, UnicodeError, ValueError) as error:
        raise SystemOptimizationInputError(
            missing_code,
            "required artifact cannot be verified and parsed",
        ) from error
    if not isinstance(payload, dict):
        raise SystemOptimizationInputError(
            missing_code,
            "required artifact is not a JSON object",
        )
    return payload


def load_authorization_profile(
    session: Session,
    cas: FileSystemCAS,
    digest: str,
    *,
    target_id: str,
) -> SystemOptimizationAuthorizationProfile:
    payload = _load_json_artifact(
        session,
        cas,
        digest,
        missing_code="authorization_profile_missing",
    )
    try:
        profile = SystemOptimizationAuthorizationProfile.model_validate(payload)
    except ValueError as error:
        raise SystemOptimizationInputError(
            "authorization_profile_invalid",
            "authorization profile does not match the required contract",
        ) from error
    if profile.target_id != target_id:
        raise SystemOptimizationInputError(
            "authorization_profile_target_mismatch",
            "authorization profile is bound to a different target",
        )
    return profile


def load_runtime_diagnostic_profile(
    session: Session,
    cas: FileSystemCAS,
    *,
    experiment_id: str,
    capacity_context_digest: str,
    target_id: str,
) -> tuple[str, RuntimeDiagnosticProfile]:
    links = list(
        session.scalars(
            select(ArtifactLinkRecord)
            .join(AttemptRecord, AttemptRecord.id == ArtifactLinkRecord.attempt_id)
            .where(
                AttemptRecord.experiment_id == experiment_id,
                AttemptRecord.status == AttemptStatus.SUCCEEDED.value,
                ArtifactLinkRecord.role == "profile",
            )
            .order_by(ArtifactLinkRecord.created_at)
        )
    )
    matches: list[tuple[str, RuntimeDiagnosticProfile]] = []
    for link in links:
        try:
            payload = _load_json_artifact(
                session,
                cas,
                link.digest,
                missing_code="runtime_profile_missing",
            )
            if payload.get("schemaVersion") != RUNTIME_PROFILE_SCHEMA:
                continue
            profile = RuntimeDiagnosticProfile.model_validate(payload)
        except (SystemOptimizationInputError, ValueError):
            continue
        if (
            profile.capacity_context_digest == capacity_context_digest
            and profile.target_id == target_id
        ):
            matches.append((link.digest, profile))
    if len(matches) != 1:
        raise SystemOptimizationInputError(
            "runtime_profile_missing",
            "capacity experiment must have exactly one context-bound runtime diagnostic profile",
            recoverable=True,
        )
    digest, profile = matches[0]
    if not any(priority.component == "storage" for priority in profile.priorities):
        raise SystemOptimizationInputError(
            "runtime_profile_missing",
            "runtime diagnostic profile does not route a measured priority to storage",
            recoverable=True,
        )
    return digest, profile


def load_runtime_diagnostic_profile_by_digest(
    session: Session,
    cas: FileSystemCAS,
    digest: str,
    *,
    target_id: str,
) -> RuntimeDiagnosticProfile:
    """Load one explicitly selected, verified profile artifact.

    The artifact must be linked with role ``profile``. The caller still binds its
    capacity context digest to the immutable baseline before creating a study.
    """
    link = session.scalar(
        select(ArtifactLinkRecord).where(
            ArtifactLinkRecord.digest == digest,
            ArtifactLinkRecord.role == "profile",
        )
    )
    if link is None:
        raise SystemOptimizationInputError(
            "runtime_profile_missing",
            "selected runtime profile is not linked as a profile artifact",
            recoverable=True,
        )
    payload = _load_json_artifact(
        session, cas, digest, missing_code="runtime_profile_missing"
    )
    try:
        profile = RuntimeDiagnosticProfile.model_validate(payload)
    except ValueError as error:
        raise SystemOptimizationInputError(
            "runtime_profile_invalid",
            "selected runtime profile does not match the required contract",
        ) from error
    if profile.target_id != target_id:
        raise SystemOptimizationInputError(
            "runtime_profile_target_mismatch",
            "selected runtime profile is bound to a different target",
        )
    if not any(priority.component == "storage" for priority in profile.priorities):
        raise SystemOptimizationInputError(
            "runtime_profile_missing",
            "runtime diagnostic profile does not route a measured priority to storage",
            recoverable=True,
        )
    return profile


__all__ = [
    "AUTHORIZATION_PROFILE_SCHEMA",
    "RUNTIME_PROFILE_SCHEMA",
    "RuntimeDiagnosticProfile",
    "SystemOptimizationAuthorizationProfile",
    "SystemOptimizationInputError",
    "load_authorization_profile",
    "load_runtime_diagnostic_profile",
    "load_runtime_diagnostic_profile_by_digest",
]
