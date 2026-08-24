"""L6 回退器：四级回退的编排与证据。

架构层：总体架构 v2 的 L6（见 docs/system-optimizer/architecture/overall.md）。
回退动作本身经 L1 安全底座执行；本模块提供回退记录合同、相位级恢复判定和
退化级通过 S8 结果向量触发；具体执行桥位于 ``rollback.regression``。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from math import isfinite
from typing import Any, Literal

from pydantic import Field, model_validator

from looper_core.canonical import canonical_digest
from looper_core.contracts import StrictModel
from looper_core.system_opt.executor import ConfigSnapshot

LEGACY_ROLLBACK_SCHEMA = "looper.rollback-record/v1alpha1"
ROLLBACK_SCHEMA = "looper.rollback-record/v1alpha2"
REGRESSION_DEPENDENCY = (
    "regression rollback requires the S8 result vector (U_regression), "
    "which is not implemented yet; see docs/system-optimizer/architecture/overall.md L6c"
)


class RollbackLevel(StrEnum):
    CANDIDATE = "candidate"
    PHASE = "phase"
    REGRESSION = "regression"
    CRASH = "crash"


class RollbackStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    NEEDS_ATTENTION = "needs-attention"


class RestorationStatus(StrEnum):
    RESTORED = "restored"
    MISMATCH = "mismatch"
    INCOMPLETE = "incomplete"


class LegacyRollbackRecord(StrictModel):
    """Historical v1alpha1 shape retained for byte-faithful evidence loading."""

    schema_version: Literal[LEGACY_ROLLBACK_SCHEMA] = LEGACY_ROLLBACK_SCHEMA
    level: RollbackLevel
    target_id: str = Field(min_length=1, max_length=160)
    item_ids: list[str] = Field(default_factory=list)
    trigger: str = Field(min_length=1, max_length=500)
    status: RollbackStatus
    verified: bool
    baseline_snapshot_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    final_snapshot_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    evidence_digests: list[str] = Field(min_length=1)
    recorded_at: datetime
    note: str | None = Field(default=None, min_length=1, max_length=1000)

    @model_validator(mode="after")
    def validate_level_semantics(self) -> LegacyRollbackRecord:
        if self.level == RollbackLevel.CANDIDATE and not self.item_ids:
            raise ValueError("candidate rollback must list the reverted item ids")
        if self.status == RollbackStatus.COMPLETED and not self.verified:
            raise ValueError("completed rollback requires a verified readback")
        if self.level == RollbackLevel.REGRESSION and self.note != REGRESSION_DEPENDENCY:
            raise ValueError(REGRESSION_DEPENDENCY)
        if len(self.evidence_digests) != len(set(self.evidence_digests)):
            raise ValueError("evidence digests must be unique")
        return self

    @property
    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json"))


class RollbackRecord(StrictModel):
    """Current rollback evidence; v1alpha2 carries executable L6c bindings."""

    schema_version: Literal[ROLLBACK_SCHEMA] = ROLLBACK_SCHEMA
    level: RollbackLevel
    target_id: str = Field(min_length=1, max_length=160)
    item_ids: list[str] = Field(default_factory=list)
    trigger: str = Field(min_length=1, max_length=500)
    status: RollbackStatus
    verified: bool
    baseline_snapshot_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    final_snapshot_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    evidence_digests: list[str] = Field(min_length=1)
    recorded_at: datetime
    note: str | None = Field(default=None, min_length=1, max_length=1000)
    checkpoint_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    regression_vector_digest: str | None = Field(
        default=None, pattern=r"^sha256:[0-9a-f]{64}$"
    )
    regression_threshold: float | None = None

    @model_validator(mode="after")
    def validate_level_semantics(self) -> RollbackRecord:
        if self.level == RollbackLevel.CANDIDATE and not self.item_ids:
            raise ValueError("candidate rollback must list the reverted item ids")
        if self.status == RollbackStatus.COMPLETED and not self.verified:
            raise ValueError("completed rollback requires a verified readback")
        if len(self.evidence_digests) != len(set(self.evidence_digests)):
            raise ValueError("evidence digests must be unique")
        for digest in self.evidence_digests:
            if not digest.startswith("sha256:"):
                raise ValueError("evidence digests must be sha256 references")

        regression_fields = (
            self.checkpoint_digest,
            self.regression_vector_digest,
            self.regression_threshold,
        )
        if self.level == RollbackLevel.REGRESSION:
            if not self.item_ids:
                raise ValueError("regression rollback must list the restored item ids")
            if any(value is None for value in regression_fields):
                raise ValueError(
                    "regression rollback requires checkpoint, result-vector, and threshold bindings"
                )
            assert self.regression_threshold is not None
            if not isfinite(self.regression_threshold):
                raise ValueError("regression rollback threshold must be finite")
            if self.baseline_snapshot_digest is None:
                raise ValueError("regression rollback requires the last-good snapshot digest")
            if self.status == RollbackStatus.COMPLETED and (
                self.final_snapshot_digest != self.baseline_snapshot_digest
            ):
                raise ValueError(
                    "completed regression rollback requires final snapshot to equal last-good"
                )
        elif any(value is not None for value in regression_fields):
            raise ValueError("regression-only bindings are forbidden on other rollback levels")
        return self

    @property
    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json"))


def load_rollback_record(
    payload: str | bytes | Mapping[str, Any],
) -> LegacyRollbackRecord | RollbackRecord:
    """Load either persisted rollback schema without rewriting historical evidence."""

    decoded: Any = (
        json.loads(payload) if isinstance(payload, (str, bytes)) else dict(payload)
    )
    if not isinstance(decoded, dict):
        raise ValueError("rollback record payload must be a JSON object")
    schema_version = decoded.get("schema_version")
    if schema_version == LEGACY_ROLLBACK_SCHEMA:
        return LegacyRollbackRecord.model_validate(decoded)
    if schema_version == ROLLBACK_SCHEMA:
        return RollbackRecord.model_validate(decoded)
    raise ValueError(f"unsupported rollback record schema: {schema_version!r}")


class PhaseRestoration(StrictModel):
    schema_version: str = "looper.phase-restoration/v1alpha1"
    status: RestorationStatus
    baseline_snapshot_digest: str
    actual_snapshot_digest: str
    differing_items: list[str]
    incomplete_items: list[str]
    missing_items: list[str]
    extra_items: list[str]
    reason: str = Field(min_length=1, max_length=1000)

    @property
    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json"))


def verify_phase_restoration(
    actual: ConfigSnapshot, baseline: ConfigSnapshot
) -> PhaseRestoration:
    """Judge whether the target actually returned to the phase baseline.

    Only a complete actual snapshot whose digest equals the complete baseline
    digest counts as restored; anything else is an explicit non-restored state.
    """

    if actual.target_id != baseline.target_id:
        raise ValueError("actual and baseline snapshots belong to different targets")
    incomplete = sorted(
        item_id
        for item_id, entry in actual.entries.items()
        if entry.status.value != "succeeded"
    )
    missing = sorted(set(baseline.entries) - set(actual.entries))
    extra = sorted(set(actual.entries) - set(baseline.entries))
    differing = sorted(
        item_id
        for item_id in set(actual.entries) & set(baseline.entries)
        if actual.entries[item_id].value != baseline.entries[item_id].value
    )
    actual_digest = actual.digest
    baseline_digest = baseline.digest
    if incomplete or missing:
        status = RestorationStatus.INCOMPLETE
        reason = (
            "actual snapshot is incomplete: "
            f"incomplete={incomplete or []}, missing={missing or []}"
        )
    elif actual_digest != baseline_digest:
        status = RestorationStatus.MISMATCH
        reason = f"actual differs from baseline: items={differing or []}, extra={extra or []}"
    else:
        status = RestorationStatus.RESTORED
        reason = "actual snapshot digest equals the complete baseline digest"
    return PhaseRestoration(
        status=status,
        baseline_snapshot_digest=baseline_digest,
        actual_snapshot_digest=actual_digest,
        differing_items=differing,
        incomplete_items=incomplete,
        missing_items=missing,
        extra_items=extra,
        reason=reason,
    )


__all__ = [
    "LEGACY_ROLLBACK_SCHEMA",
    "LegacyRollbackRecord",
    "PhaseRestoration",
    "REGRESSION_DEPENDENCY",
    "ROLLBACK_SCHEMA",
    "RestorationStatus",
    "RollbackLevel",
    "RollbackRecord",
    "RollbackStatus",
    "load_rollback_record",
    "verify_phase_restoration",
]
