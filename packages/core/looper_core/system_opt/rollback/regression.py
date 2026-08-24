"""L6c runtime-regression recovery against an S9-validated checkpoint.

This module consumes an already-normalized S8 result vector and an explicit
task threshold.  It never derives normalization, thresholds, or a last-good
configuration.  A checkpoint is eligible only when it carries successful S9
promotion evidence and a complete configuration snapshot.

The actual write path is delegated to L1 ``SafetyController``.  A triggered
recovery always stops the current search: exact round-trip restoration produces
``restored``; every other result is ``needs-attention``.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from looper_core.canonical import canonical_digest
from looper_core.contracts import StrictModel
from looper_core.system_opt.config_manifest import ConfigManifest
from looper_core.system_opt.executor import ConfigSnapshot, ExecutorBackend
from looper_core.system_opt.result_vector import (
    GeneralResultVector,
    PromotionEvidence,
    regression_triggered,
)
from looper_core.system_opt.rollback import (
    PhaseRestoration,
    RestorationStatus,
    RollbackLevel,
    RollbackRecord,
    RollbackStatus,
    verify_phase_restoration,
)
from looper_core.system_opt.safety import SafetyController, SafetyResult, SafetyState

LAST_GOOD_CHECKPOINT_SCHEMA = "looper.last-good-checkpoint/v1alpha1"
REGRESSION_RECOVERY_REQUEST_SCHEMA = "looper.regression-recovery-request/v1alpha1"
REGRESSION_EXECUTION_EVIDENCE_SCHEMA = "looper.regression-execution-evidence/v1alpha1"
REGRESSION_RECOVERY_OUTCOME_SCHEMA = "looper.regression-recovery-outcome/v1alpha1"
_DIGEST = r"^sha256:[0-9a-f]{64}$"


class RegressionRecoveryStatus(StrEnum):
    NOT_TRIGGERED = "not-triggered"
    RESTORED = "restored"
    NEEDS_ATTENTION = "needs-attention"


class LastGoodCheckpoint(StrictModel):
    """Exact configuration checkpoint proven eligible by S9 promotion."""

    schema_version: Literal[LAST_GOOD_CHECKPOINT_SCHEMA] = LAST_GOOD_CHECKPOINT_SCHEMA
    target_id: str = Field(min_length=1, max_length=200)
    candidate_id: str = Field(min_length=1, max_length=200)
    snapshot: ConfigSnapshot
    promotion_evidence: PromotionEvidence
    validated_vector: GeneralResultVector
    recorded_at: datetime

    @model_validator(mode="after")
    def validate_s9_binding(self) -> LastGoodCheckpoint:
        if self.snapshot.target_id != self.target_id:
            raise ValueError("last-good snapshot belongs to a different target")
        if not self.snapshot.complete:
            raise ValueError("last-good snapshot must be complete and non-empty")
        for item_id, entry in self.snapshot.entries.items():
            if entry.item_id != item_id:
                raise ValueError("last-good snapshot keys must match embedded item ids")
        if not self.promotion_evidence.promoted:
            raise ValueError("last-good checkpoint requires promoted S9 evidence")
        if self.promotion_evidence.failed_observations:
            raise ValueError("promoted S9 evidence cannot retain failed observations")
        if self.promotion_evidence.candidate_id != self.candidate_id:
            raise ValueError("S9 promotion candidate does not match last-good candidate")
        if self.validated_vector.candidate_id != self.candidate_id:
            raise ValueError("validated S8 vector does not match last-good candidate")
        return self

    @property
    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json", exclude_none=False))


class RegressionRecoveryRequest(StrictModel):
    """Explicit L6c trigger input; every threshold and evidence input is bound."""

    schema_version: Literal[REGRESSION_RECOVERY_REQUEST_SCHEMA] = (
        REGRESSION_RECOVERY_REQUEST_SCHEMA
    )
    checkpoint: LastGoodCheckpoint
    current_vector: GeneralResultVector
    regression_threshold: float
    trigger_evidence_digests: list[str] = Field(min_length=1)
    evaluated_at: datetime

    @model_validator(mode="after")
    def validate_identity(self) -> RegressionRecoveryRequest:
        if self.current_vector.candidate_id != self.checkpoint.candidate_id:
            raise ValueError("runtime S8 vector does not match last-good candidate")
        if (
            self.current_vector.normalization_digest
            != self.checkpoint.validated_vector.normalization_digest
        ):
            raise ValueError("runtime and last-good S8 vectors use different normalization")
        if len(self.trigger_evidence_digests) != len(set(self.trigger_evidence_digests)):
            raise ValueError("regression trigger evidence digests must be unique")
        for digest in self.trigger_evidence_digests:
            if not digest.startswith("sha256:"):
                raise ValueError("regression trigger evidence must use sha256 references")
        # Reuse the registered S8 trigger validator, including finite-threshold checks.
        regression_triggered(self.current_vector, threshold=self.regression_threshold)
        return self

    @property
    def triggered(self) -> bool:
        return regression_triggered(
            self.current_vector,
            threshold=self.regression_threshold,
        )

    @property
    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json", exclude_none=False))


class RegressionExecutionEvidence(StrictModel):
    """Replayable L1 execution and exact-restoration evidence."""

    schema_version: Literal[REGRESSION_EXECUTION_EVIDENCE_SCHEMA] = (
        REGRESSION_EXECUTION_EVIDENCE_SCHEMA
    )
    request_digest: str = Field(pattern=_DIGEST)
    safety_result: SafetyResult | None = None
    restoration: PhaseRestoration | None = None
    execution_error_type: str | None = Field(default=None, min_length=1, max_length=200)
    execution_error_message: str | None = Field(default=None, min_length=1, max_length=2000)

    @model_validator(mode="after")
    def validate_execution_shape(self) -> RegressionExecutionEvidence:
        error_fields = (self.execution_error_type, self.execution_error_message)
        if (error_fields[0] is None) != (error_fields[1] is None):
            raise ValueError("execution error type and message must be recorded together")
        if self.safety_result is None and self.execution_error_type is None:
            raise ValueError("execution evidence requires a safety result or explicit error")
        if self.restoration is not None and self.safety_result is None:
            raise ValueError("restoration evidence requires an L1 safety result")
        return self

    @property
    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json", exclude_none=False))


class RegressionRecoveryOutcome(StrictModel):
    """L6c terminal result for one runtime regression evaluation."""

    schema_version: Literal[REGRESSION_RECOVERY_OUTCOME_SCHEMA] = (
        REGRESSION_RECOVERY_OUTCOME_SCHEMA
    )
    request_digest: str = Field(pattern=_DIGEST)
    status: RegressionRecoveryStatus
    stop_required: bool
    reason: str = Field(min_length=1, max_length=1000)
    execution_evidence: RegressionExecutionEvidence | None = None
    rollback_record: RollbackRecord | None = None

    @model_validator(mode="after")
    def validate_terminal_semantics(self) -> RegressionRecoveryOutcome:
        if self.status is RegressionRecoveryStatus.NOT_TRIGGERED:
            if self.stop_required:
                raise ValueError("non-triggered regression evaluation cannot stop the search")
            if self.execution_evidence is not None or self.rollback_record is not None:
                raise ValueError("non-triggered regression evaluation cannot carry execution data")
            return self

        if not self.stop_required:
            raise ValueError("triggered regression recovery must stop the current search")
        if self.execution_evidence is None or self.rollback_record is None:
            raise ValueError(
                "triggered regression recovery requires execution and rollback evidence"
            )
        if self.rollback_record.level is not RollbackLevel.REGRESSION:
            raise ValueError("regression outcome requires a regression-level rollback record")
        if self.status is RegressionRecoveryStatus.RESTORED:
            if self.rollback_record.status is not RollbackStatus.COMPLETED:
                raise ValueError("restored outcome requires a completed rollback record")
        elif self.rollback_record.status is not RollbackStatus.NEEDS_ATTENTION:
            raise ValueError("failed regression recovery must require attention")
        return self

    @property
    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json", exclude_none=False))


def _bounded_reason(value: str) -> str:
    return value if len(value) <= 1000 else value[:997] + "..."


def _restoration_values(
    checkpoint: LastGoodCheckpoint,
    manifest: ConfigManifest,
    backend: ExecutorBackend,
) -> dict[str, object]:
    if backend.capabilities.target_id != checkpoint.target_id:
        raise ValueError("executor target does not match last-good checkpoint")
    values: dict[str, object] = {}
    for item_id, entry in checkpoint.snapshot.entries.items():
        item = manifest.item(item_id)
        if entry.target != item.target:
            raise ValueError(f"last-good target binding differs for item {item_id!r}")
        item.validate_value(entry.value)
        values[item.parameter_id] = entry.value
    return values


def execute_regression_recovery(
    request: RegressionRecoveryRequest,
    *,
    manifest: ConfigManifest,
    controller: SafetyController,
    backend: ExecutorBackend,
    fencing_token: int,
    recorded_at: datetime,
) -> RegressionRecoveryOutcome:
    """Restore an exact S9 checkpoint when the explicit S8 floor is crossed."""

    if not request.triggered:
        return RegressionRecoveryOutcome(
            request_digest=request.digest,
            status=RegressionRecoveryStatus.NOT_TRIGGERED,
            stop_required=False,
            reason=(
                f"u_regression={request.current_vector.u_regression} is not below "
                f"the explicit threshold {request.regression_threshold}"
            ),
        )

    safety_result: SafetyResult | None = None
    restoration: PhaseRestoration | None = None
    execution_error: Exception | None = None
    try:
        restoration_values = _restoration_values(request.checkpoint, manifest, backend)
        safety_result = controller.execute(
            manifest,
            restoration_values,
            backend,
            fencing_token=fencing_token,
            keep=True,
            keep_authorized=True,
        )
        if safety_result.state is SafetyState.KEPT and safety_result.final_snapshot is not None:
            restoration = verify_phase_restoration(
                safety_result.final_snapshot,
                request.checkpoint.snapshot,
            )
    except Exception as error:  # L6 is a fail-closed safety boundary
        execution_error = error

    execution_evidence = RegressionExecutionEvidence(
        request_digest=request.digest,
        safety_result=safety_result,
        restoration=restoration,
        execution_error_type=(type(execution_error).__name__ if execution_error else None),
        execution_error_message=(str(execution_error) if execution_error else None),
    )
    restored = (
        safety_result is not None
        and safety_result.state is SafetyState.KEPT
        and restoration is not None
        and restoration.status is RestorationStatus.RESTORED
    )
    if restored:
        status = RegressionRecoveryStatus.RESTORED
        record_status = RollbackStatus.COMPLETED
        reason = "exact S9-validated last-good snapshot restored and verified"
    else:
        status = RegressionRecoveryStatus.NEEDS_ATTENTION
        record_status = RollbackStatus.NEEDS_ATTENTION
        if execution_error is not None:
            reason = (
                "regression recovery execution failed: "
                f"{type(execution_error).__name__}: {execution_error}"
            )
        elif safety_result is not None and safety_result.state is not SafetyState.KEPT:
            reason = (
                f"L1 recovery did not keep last-good state: {safety_result.state.value}: "
                f"{safety_result.reason}"
            )
        elif restoration is not None:
            reason = f"last-good round-trip verification failed: {restoration.reason}"
        else:
            reason = "last-good recovery produced no verifiable final snapshot"

    final_snapshot_digest = (
        safety_result.final_snapshot.digest
        if safety_result is not None and safety_result.final_snapshot is not None
        else None
    )
    rollback_record = RollbackRecord(
        level=RollbackLevel.REGRESSION,
        target_id=request.checkpoint.target_id,
        item_ids=sorted(request.checkpoint.snapshot.entries),
        trigger=(
            f"u_regression={request.current_vector.u_regression} fell below explicit "
            f"threshold {request.regression_threshold}"
        ),
        status=record_status,
        verified=restored,
        baseline_snapshot_digest=request.checkpoint.snapshot.digest,
        final_snapshot_digest=final_snapshot_digest,
        evidence_digests=[request.digest, execution_evidence.digest],
        recorded_at=recorded_at,
        note=_bounded_reason(reason),
        checkpoint_digest=request.checkpoint.digest,
        regression_vector_digest=request.current_vector.digest,
        regression_threshold=request.regression_threshold,
    )
    return RegressionRecoveryOutcome(
        request_digest=request.digest,
        status=status,
        stop_required=True,
        reason=_bounded_reason(reason),
        execution_evidence=execution_evidence,
        rollback_record=rollback_record,
    )


__all__ = [
    "LAST_GOOD_CHECKPOINT_SCHEMA",
    "REGRESSION_EXECUTION_EVIDENCE_SCHEMA",
    "REGRESSION_RECOVERY_OUTCOME_SCHEMA",
    "REGRESSION_RECOVERY_REQUEST_SCHEMA",
    "LastGoodCheckpoint",
    "RegressionExecutionEvidence",
    "RegressionRecoveryOutcome",
    "RegressionRecoveryRequest",
    "RegressionRecoveryStatus",
    "execute_regression_recovery",
]
