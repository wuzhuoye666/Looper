"""L1 安全执行原语：preflight → snapshot → apply → verify → rollback（fail-closed）。

架构层：L1（docs/system-optimizer/architecture/overall.md）。
多接口修改是补偿事务，不宣称内核级原子；回退后必须读回验证，失败进入 needs-attention。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from enum import StrEnum
from typing import Any, Literal

from pydantic import Field, model_validator

from looper_core.canonical import canonical_digest, canonical_json
from looper_core.contracts import StrictModel
from looper_core.system_opt.config_manifest import (
    ActivationMode,
    ConfigManifest,
    RiskLevel,
)
from looper_core.system_opt.executor import (
    ConfigSnapshot,
    ExecutorBackend,
    OperationResult,
)

OBSERVED_SAFETY_RESULT_SCHEMA = "looper.observed-safety-result/v1alpha1"


def _bounded_error_message(error: BaseException) -> str:
    message = str(error).strip() or "no error message"
    return message[:2000]


def _bounded_exception(error: BaseException) -> str:
    return f"{type(error).__name__}: {_bounded_error_message(error)}"[:2000]


class SafetyState(StrEnum):
    PREFLIGHT = "preflight"
    REJECTED = "rejected"
    SNAPSHOT = "snapshot"
    APPLY = "apply"
    VERIFY = "verify"
    MEASURE = "measure"
    ROLLBACK = "rollback"
    ROLLED_BACK = "rolled_back"
    KEPT = "kept"
    NEEDS_ATTENTION = "needs-attention"


class SafetyProgressStage(StrEnum):
    """Durable observation seams around one L1 safety execution."""

    PREFLIGHT_COMPLETED = "preflight-completed"
    APPLY_STARTED = "apply-started"
    ROLLBACK_STARTED = "rollback-started"
    ROLLBACK_VERIFIED = "rollback-verified"
    SAFETY_TERMINAL = "safety-terminal"


class MeasurementStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMEOUT = "timeout"


class MeasurementResult(StrictModel):
    status: MeasurementStatus
    reason: str | None = None
    evidence_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")


class SafetyPolicy(StrictModel):
    max_changes: int = Field(default=5, ge=1, le=100)
    max_changes_reason: str | None = Field(default=None, min_length=1, max_length=1000)
    pinned_items: set[str] = Field(default_factory=set)
    ownership_unknown_items: set[str] = Field(default_factory=set)
    high_risk_waivers: set[str] = Field(default_factory=set)
    allow_keep: bool = False
    require_privileged: bool = True

    @model_validator(mode="after")
    def validate_change_limit(self) -> SafetyPolicy:
        if self.max_changes > 5 and not self.max_changes_reason:
            raise ValueError("raising max_changes above 5 requires an explicit reason")
        overlap = self.pinned_items & self.ownership_unknown_items
        if overlap:
            raise ValueError(
                f"items cannot be both pinned and ownership-unknown: {sorted(overlap)}"
            )
        return self


class SafetyEvent(StrictModel):
    sequence: int = Field(ge=0)
    state: SafetyState
    operation: str
    status: str
    item_id: str | None = None
    old_value: Any | None = None
    requested_value: Any | None = None
    readback_value: Any | None = None
    reason: str | None = None
    snapshot_digest: str | None = None
    round_trip_digest: str | None = None


class SafetyResult(StrictModel):
    state: SafetyState
    events: tuple[SafetyEvent, ...]
    snapshot: ConfigSnapshot | None = None
    final_snapshot: ConfigSnapshot | None = None
    applied_items: list[str] = Field(default_factory=list)
    reason: str | None = None

    @property
    def needs_attention(self) -> bool:
        return self.state == SafetyState.NEEDS_ATTENTION

    @property
    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json", exclude_none=False))


class SafetyProgressEvent(StrictModel):
    stage: SafetyProgressStage
    safety_state: SafetyState
    item_id: str | None = None
    operation: str | None = None
    evidence_digest: str | None = Field(
        default=None, pattern=r"^sha256:[0-9a-f]{64}$"
    )

    @model_validator(mode="after")
    def validate_terminal_evidence(self) -> SafetyProgressEvent:
        if self.stage is SafetyProgressStage.SAFETY_TERMINAL:
            if self.evidence_digest is None:
                raise ValueError("safety-terminal progress requires an evidence digest")
        elif self.evidence_digest is not None:
            raise ValueError("only safety-terminal progress may carry evidence")
        return self


class SafetyProgressFailure(StrictModel):
    stage: SafetyProgressStage
    error_type: str = Field(min_length=1, max_length=200)
    error_message: str = Field(min_length=1, max_length=2000)


class ObservedSafetyResult(StrictModel):
    schema_version: Literal[OBSERVED_SAFETY_RESULT_SCHEMA] = OBSERVED_SAFETY_RESULT_SCHEMA
    result: SafetyResult
    progress_failures: list[SafetyProgressFailure] = Field(default_factory=list)

    @property
    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json", exclude_none=False))


class ProgressRecordError(RuntimeError):
    """Raised when progress cannot be recorded before a backend write can begin."""


MeasureCallback = Callable[[], MeasurementResult]
ProgressObserver = Callable[[SafetyProgressEvent], None]


class SafetyController:
    def __init__(self, policy: SafetyPolicy | None = None) -> None:
        self.policy = policy or SafetyPolicy()

    def execute(
        self,
        manifest: ConfigManifest,
        candidate_values: Mapping[str, Any],
        backend: ExecutorBackend,
        *,
        fencing_token: int,
        measure: MeasureCallback | None = None,
        keep: bool = False,
        keep_authorized: bool = False,
    ) -> SafetyResult:
        result, _ = self._execute(
            manifest,
            candidate_values,
            backend,
            fencing_token=fencing_token,
            measure=measure,
            keep=keep,
            keep_authorized=keep_authorized,
            progress_observer=None,
        )
        return result

    def execute_observed(
        self,
        manifest: ConfigManifest,
        candidate_values: Mapping[str, Any],
        backend: ExecutorBackend,
        *,
        fencing_token: int,
        progress_observer: ProgressObserver,
        measure: MeasureCallback | None = None,
        keep: bool = False,
        keep_authorized: bool = False,
    ) -> ObservedSafetyResult:
        result, failures = self._execute(
            manifest,
            candidate_values,
            backend,
            fencing_token=fencing_token,
            measure=measure,
            keep=keep,
            keep_authorized=keep_authorized,
            progress_observer=progress_observer,
        )
        return ObservedSafetyResult(result=result, progress_failures=failures)

    def _execute(
        self,
        manifest: ConfigManifest,
        candidate_values: Mapping[str, Any],
        backend: ExecutorBackend,
        *,
        fencing_token: int,
        measure: MeasureCallback | None,
        keep: bool,
        keep_authorized: bool,
        progress_observer: ProgressObserver | None,
    ) -> tuple[SafetyResult, list[SafetyProgressFailure]]:
        events: list[SafetyEvent] = []
        progress_failures: list[SafetyProgressFailure] = []
        apply_started_recorded = False
        progress_tainted = False

        def progress(
            stage: SafetyProgressStage,
            safety_state: SafetyState,
            *,
            item_id: str | None = None,
            operation: str | None = None,
            evidence_digest: str | None = None,
        ) -> None:
            nonlocal apply_started_recorded, progress_tainted
            if progress_observer is None:
                if stage is SafetyProgressStage.APPLY_STARTED:
                    apply_started_recorded = True
                return
            if progress_tainted:
                progress_failures.append(
                    SafetyProgressFailure(
                        stage=stage,
                        error_type="ProgressChannelTainted",
                        error_message="a prior post-apply progress write failed",
                    )
                )
                return
            record = SafetyProgressEvent(
                stage=stage,
                safety_state=safety_state,
                item_id=item_id,
                operation=operation,
                evidence_digest=evidence_digest,
            )
            try:
                progress_observer(record)
            except Exception as error:
                if not apply_started_recorded:
                    raise ProgressRecordError(
                        f"failed to record {stage.value} before backend apply"
                    ) from error
                progress_tainted = True
                progress_failures.append(
                    SafetyProgressFailure(
                        stage=stage,
                        error_type=type(error).__name__[:200],
                        error_message=_bounded_error_message(error),
                    )
                )
                return
            if stage is SafetyProgressStage.APPLY_STARTED:
                apply_started_recorded = True

        def finish(result: SafetyResult) -> tuple[SafetyResult, list[SafetyProgressFailure]]:
            progress(
                SafetyProgressStage.SAFETY_TERMINAL,
                result.state,
                operation="safety-terminal",
                evidence_digest=result.digest,
            )
            return result, progress_failures

        def event(
            state: SafetyState,
            operation: str,
            status: str,
            *,
            item_id: str | None = None,
            old_value: Any | None = None,
            requested_value: Any | None = None,
            readback_value: Any | None = None,
            reason: str | None = None,
            snapshot_digest: str | None = None,
            round_trip_digest: str | None = None,
        ) -> None:
            events.append(
                SafetyEvent(
                    sequence=len(events),
                    state=state,
                    operation=operation,
                    status=status,
                    item_id=item_id,
                    old_value=old_value,
                    requested_value=requested_value,
                    readback_value=readback_value,
                    reason=reason,
                    snapshot_digest=snapshot_digest,
                    round_trip_digest=round_trip_digest,
                )
            )

        try:
            selected_items = {
                manifest.item_for_parameter(parameter_id).id: manifest.item_for_parameter(
                    parameter_id
                )
                for parameter_id in candidate_values
            }
        except KeyError as error:
            reason = f"candidate contains an unknown system parameter: {error.args[0]}"
            event(SafetyState.PREFLIGHT, "preflight", "failed", reason=reason)
            return finish(
                SafetyResult(state=SafetyState.REJECTED, events=tuple(events), reason=reason)
            )

        try:
            preflight_error = self._preflight(
                selected_items,
                candidate_values,
                backend,
            )
        except Exception as error:
            preflight_error = f"backend preflight raised: {_bounded_exception(error)}"
        if preflight_error:
            event(SafetyState.PREFLIGHT, "preflight", "failed", reason=preflight_error)
            return finish(
                SafetyResult(
                    state=SafetyState.REJECTED,
                    events=tuple(events),
                    reason=preflight_error,
                )
            )
        event(SafetyState.PREFLIGHT, "preflight", "succeeded")
        progress(
            SafetyProgressStage.PREFLIGHT_COMPLETED,
            SafetyState.PREFLIGHT,
            operation="preflight",
        )

        ordered = manifest.ordered_items(set(selected_items))
        try:
            snapshot = backend.snapshot(ordered, fencing_token=fencing_token)
        except Exception as error:
            reason = f"backend baseline snapshot raised: {_bounded_exception(error)}"
            event(SafetyState.SNAPSHOT, "snapshot", "failed", reason=reason)
            return finish(
                SafetyResult(
                    state=SafetyState.REJECTED,
                    events=tuple(events),
                    reason=reason,
                )
            )
        event(
            SafetyState.SNAPSHOT,
            "snapshot",
            "succeeded" if snapshot.complete else "failed",
            snapshot_digest=snapshot.digest,
            reason=None if snapshot.complete else "snapshot is incomplete",
        )
        if not snapshot.complete:
            return finish(
                SafetyResult(
                    state=SafetyState.REJECTED,
                    events=tuple(events),
                    snapshot=snapshot,
                    reason="snapshot is incomplete",
                )
            )

        applied: list[str] = []
        for item in ordered:
            requested = candidate_values[item.parameter_id]
            old_value = snapshot.entries[item.id].value
            if canonical_json(old_value) == canonical_json(requested):
                event(
                    SafetyState.APPLY,
                    "apply",
                    "unchanged",
                    item_id=item.id,
                    old_value=old_value,
                    requested_value=requested,
                    readback_value=old_value,
                )
                continue
            # Once apply starts, the target may have changed even when the backend
            # reports failed/timeout/unknown. Include the current item in compensation.
            if not applied:
                progress(
                    SafetyProgressStage.APPLY_STARTED,
                    SafetyState.APPLY,
                    item_id=item.id,
                    operation="apply",
                )
            applied.append(item.id)
            try:
                result = backend.apply(item, requested, fencing_token=fencing_token)
            except Exception as error:
                reason = f"backend apply raised for {item.id}: {_bounded_exception(error)}"
                event(
                    SafetyState.APPLY,
                    "apply",
                    "failed",
                    item_id=item.id,
                    old_value=old_value,
                    requested_value=requested,
                    reason=reason,
                )
                rolled_back = self._rollback(
                    manifest,
                    backend,
                    snapshot,
                    applied,
                    fencing_token,
                    events,
                    reason=reason,
                    progress=progress,
                )
                return finish(rolled_back)
            self._operation_event(events, SafetyState.APPLY, result)
            if not result.succeeded:
                rolled_back = self._rollback(
                    manifest,
                    backend,
                    snapshot,
                    applied,
                    fencing_token,
                    events,
                    reason=f"apply failed for {item.id}",
                    progress=progress,
                )
                return finish(rolled_back)

        for item in ordered:
            if item.id not in applied:
                continue
            expected = candidate_values[item.parameter_id]
            try:
                result = backend.verify(item, expected, fencing_token=fencing_token)
            except Exception as error:
                reason = f"backend verify raised for {item.id}: {_bounded_exception(error)}"
                event(
                    SafetyState.VERIFY,
                    "verify",
                    "failed",
                    item_id=item.id,
                    requested_value=expected,
                    reason=reason,
                )
                rolled_back = self._rollback(
                    manifest,
                    backend,
                    snapshot,
                    applied,
                    fencing_token,
                    events,
                    reason=reason,
                    progress=progress,
                )
                return finish(rolled_back)
            self._operation_event(events, SafetyState.VERIFY, result)
            if not result.succeeded:
                rolled_back = self._rollback(
                    manifest,
                    backend,
                    snapshot,
                    applied,
                    fencing_token,
                    events,
                    reason=f"verify failed for {item.id}",
                    progress=progress,
                )
                return finish(rolled_back)

        try:
            measurement = (
                measure()
                if measure is not None
                else MeasurementResult(status=MeasurementStatus.SUCCEEDED)
            )
        except TimeoutError as error:
            measurement = MeasurementResult(status=MeasurementStatus.TIMEOUT, reason=str(error))
        except Exception as error:  # the safety boundary must compensate arbitrary workload errors
            measurement = MeasurementResult(status=MeasurementStatus.FAILED, reason=str(error))
        event(
            SafetyState.MEASURE,
            "measure",
            measurement.status.value,
            reason=measurement.reason,
        )
        if measurement.status != MeasurementStatus.SUCCEEDED:
            rolled_back = self._rollback(
                manifest,
                backend,
                snapshot,
                applied,
                fencing_token,
                events,
                reason=f"measurement {measurement.status.value}",
                progress=progress,
            )
            return finish(rolled_back)

        if keep and self.policy.allow_keep and keep_authorized:
            try:
                final_snapshot = backend.snapshot(ordered, fencing_token=fencing_token)
            except Exception as error:
                reason = f"backend kept-state snapshot raised: {_bounded_exception(error)}"
                event(SafetyState.SNAPSHOT, "snapshot", "failed", reason=reason)
                rolled_back = self._rollback(
                    manifest,
                    backend,
                    snapshot,
                    applied,
                    fencing_token,
                    events,
                    reason=reason,
                    progress=progress,
                )
                return finish(rolled_back)
            if not final_snapshot.complete:
                rolled_back = self._rollback(
                    manifest,
                    backend,
                    snapshot,
                    applied,
                    fencing_token,
                    events,
                    reason="kept state could not be verified",
                    progress=progress,
                )
                return finish(rolled_back)
            event(
                SafetyState.KEPT,
                "keep",
                "succeeded",
                round_trip_digest=final_snapshot.digest,
            )
            return finish(
                SafetyResult(
                    state=SafetyState.KEPT,
                    events=tuple(events),
                    snapshot=snapshot,
                    final_snapshot=final_snapshot,
                    applied_items=applied,
                )
            )

        keep_reason = None
        if keep:
            keep_reason = "keep was requested without both policy and explicit authorization"
        rolled_back = self._rollback(
            manifest,
            backend,
            snapshot,
            applied,
            fencing_token,
            events,
            reason=keep_reason or "default rollback after measurement",
            progress=progress,
        )
        return finish(rolled_back)

    def _preflight(
        self,
        selected_items: Mapping[str, Any],
        candidate_values: Mapping[str, Any],
        backend: ExecutorBackend,
    ) -> str | None:
        if not backend.capabilities.enabled:
            return f"backend {backend.capabilities.kind.value} is disabled"
        if not backend.capabilities.supports_fencing:
            return "backend does not support fencing"
        if self.policy.require_privileged and not backend.capabilities.privileged:
            return "backend has not declared privileged execution capability"
        if len(selected_items) > self.policy.max_changes:
            return (
                f"candidate declares {len(selected_items)} changes; "
                f"limit is {self.policy.max_changes}"
            )
        for item in selected_items.values():
            if item.category not in backend.capabilities.categories:
                return f"backend does not support category {item.category.value}"
            if item.permanently_blacklisted:
                return f"{item.id} targets a permanent safety blacklist"
            if item.activation != ActivationMode.IMMEDIATE or not item.searchable:
                return f"{item.id} is observation-only"
            if item.id in self.policy.pinned_items:
                return f"{item.id} is pinned and cannot be changed"
            if item.id in self.policy.ownership_unknown_items:
                return f"{item.id} ownership is unknown and cannot be changed"
            if item.risk == RiskLevel.HIGH and item.id not in self.policy.high_risk_waivers:
                return f"{item.id} is high-risk and has no runtime waiver"
            check = backend.preflight_check(item)
            if not check.succeeded:
                return check.message or f"preflight check failed for {item.id}"
            try:
                item.validate_value(candidate_values[item.parameter_id])
            except ValueError as error:
                return str(error)
        return None

    @staticmethod
    def _operation_event(
        events: list[SafetyEvent], state: SafetyState, result: OperationResult
    ) -> None:
        events.append(
            SafetyEvent(
                sequence=len(events),
                state=state,
                operation=result.operation,
                status=result.status.value,
                item_id=result.item_id,
                old_value=result.old_value,
                requested_value=result.requested_value,
                readback_value=result.readback_value,
                reason=result.message,
            )
        )

    def _rollback(
        self,
        manifest: ConfigManifest,
        backend: ExecutorBackend,
        snapshot: ConfigSnapshot,
        applied: list[str],
        fencing_token: int,
        events: list[SafetyEvent],
        *,
        reason: str,
        progress: Callable[..., None],
    ) -> SafetyResult:
        item_by_id = {item.id: item for item in manifest.items}
        rollback_failed = False
        if applied:
            progress(
                SafetyProgressStage.ROLLBACK_STARTED,
                SafetyState.ROLLBACK,
                item_id=applied[-1],
                operation="rollback",
            )
        for item_id in reversed(applied):
            item = item_by_id[item_id]
            snapshot_value = snapshot.entries[item_id].value
            try:
                result = backend.rollback(item, snapshot_value, fencing_token=fencing_token)
            except Exception as error:
                rollback_failed = True
                events.append(
                    SafetyEvent(
                        sequence=len(events),
                        state=SafetyState.NEEDS_ATTENTION,
                        operation="rollback",
                        status="failed",
                        item_id=item.id,
                        requested_value=snapshot_value,
                        reason=(
                            f"backend rollback raised: {_bounded_exception(error)}"
                        ),
                    )
                )
                continue
            self._operation_event(events, SafetyState.ROLLBACK, result)
            if not result.succeeded:
                rollback_failed = True
                events.append(
                    SafetyEvent(
                        sequence=len(events),
                        state=SafetyState.NEEDS_ATTENTION,
                        operation="rollback_failed",
                        status="failed",
                        item_id=item.id,
                        requested_value=snapshot_value,
                        reason=result.message or "rollback command failed",
                    )
                )
                continue
            try:
                verify = backend.verify(item, snapshot_value, fencing_token=fencing_token)
            except Exception as error:
                rollback_failed = True
                events.append(
                    SafetyEvent(
                        sequence=len(events),
                        state=SafetyState.NEEDS_ATTENTION,
                        operation="rollback-verify",
                        status="failed",
                        item_id=item.id,
                        requested_value=snapshot_value,
                        reason=(
                            f"backend rollback verify raised: {_bounded_exception(error)}"
                        ),
                    )
                )
                continue
            self._operation_event(events, SafetyState.ROLLBACK, verify)
            if not verify.succeeded:
                rollback_failed = True
                events.append(
                    SafetyEvent(
                        sequence=len(events),
                        state=SafetyState.NEEDS_ATTENTION,
                        operation="rollback_failed",
                        status="failed",
                        item_id=item.id,
                        requested_value=snapshot_value,
                        readback_value=verify.readback_value,
                        reason=verify.message or "rollback verification failed",
                    )
                )

        ordered = manifest.ordered_items(set(snapshot.entries))
        try:
            final_snapshot = backend.snapshot(ordered, fencing_token=fencing_token)
        except Exception as error:
            final_snapshot = None
            round_trip_matches = False
            events.append(
                SafetyEvent(
                    sequence=len(events),
                    state=SafetyState.NEEDS_ATTENTION,
                    operation="rollback-snapshot",
                    status="failed",
                    reason=(
                        f"backend rollback snapshot raised: {_bounded_exception(error)}"
                    ),
                    snapshot_digest=snapshot.digest,
                )
            )
        else:
            round_trip_matches = (
                final_snapshot.complete and final_snapshot.digest == snapshot.digest
            )
        if not round_trip_matches:
            rollback_failed = True
            if final_snapshot is not None:
                events.append(
                    SafetyEvent(
                        sequence=len(events),
                        state=SafetyState.NEEDS_ATTENTION,
                        operation="rollback_failed",
                        status="failed",
                        reason="round-trip snapshot digest does not match the baseline",
                        snapshot_digest=snapshot.digest,
                        round_trip_digest=final_snapshot.digest,
                    )
                )
        elif applied:
            progress(
                SafetyProgressStage.ROLLBACK_VERIFIED,
                SafetyState.ROLLED_BACK,
                operation="rollback-verify",
            )
        state = SafetyState.NEEDS_ATTENTION if rollback_failed else SafetyState.ROLLED_BACK
        events.append(
            SafetyEvent(
                sequence=len(events),
                state=state,
                operation="rollback-complete",
                status="failed" if rollback_failed else "succeeded",
                reason=reason,
                snapshot_digest=snapshot.digest,
                round_trip_digest=(
                    final_snapshot.digest if final_snapshot is not None else None
                ),
            )
        )
        return SafetyResult(
            state=state,
            events=tuple(events),
            snapshot=snapshot,
            final_snapshot=final_snapshot,
            applied_items=list(applied),
            reason=reason,
        )


__all__ = [
    "MeasurementResult",
    "MeasurementStatus",
    "OBSERVED_SAFETY_RESULT_SCHEMA",
    "ObservedSafetyResult",
    "ProgressObserver",
    "ProgressRecordError",
    "SafetyController",
    "SafetyEvent",
    "SafetyPolicy",
    "SafetyProgressEvent",
    "SafetyProgressFailure",
    "SafetyProgressStage",
    "SafetyResult",
    "SafetyState",
]
