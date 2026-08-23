"""L1 安全执行原语：preflight → snapshot → apply → verify → rollback（fail-closed）。

架构层：L1（docs/system-optimizer/architecture/overall.md）。
多接口修改是补偿事务，不宣称内核级原子；回退后必须读回验证，失败进入 needs-attention。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from enum import StrEnum
from typing import Any

from pydantic import Field, model_validator

from looper_core.canonical import canonical_json
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


MeasureCallback = Callable[[], MeasurementResult]


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
        events: list[SafetyEvent] = []

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
            return SafetyResult(state=SafetyState.REJECTED, events=tuple(events), reason=reason)

        preflight_error = self._preflight(
            selected_items,
            candidate_values,
            backend,
        )
        if preflight_error:
            event(SafetyState.PREFLIGHT, "preflight", "failed", reason=preflight_error)
            return SafetyResult(
                state=SafetyState.REJECTED, events=tuple(events), reason=preflight_error
            )
        event(SafetyState.PREFLIGHT, "preflight", "succeeded")

        ordered = manifest.ordered_items(set(selected_items))
        snapshot = backend.snapshot(ordered, fencing_token=fencing_token)
        event(
            SafetyState.SNAPSHOT,
            "snapshot",
            "succeeded" if snapshot.complete else "failed",
            snapshot_digest=snapshot.digest,
            reason=None if snapshot.complete else "snapshot is incomplete",
        )
        if not snapshot.complete:
            return SafetyResult(
                state=SafetyState.REJECTED,
                events=tuple(events),
                snapshot=snapshot,
                reason="snapshot is incomplete",
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
            applied.append(item.id)
            result = backend.apply(item, requested, fencing_token=fencing_token)
            self._operation_event(events, SafetyState.APPLY, result)
            if not result.succeeded:
                return self._rollback(
                    manifest,
                    backend,
                    snapshot,
                    applied,
                    fencing_token,
                    events,
                    reason=f"apply failed for {item.id}",
                )

        for item in ordered:
            if item.id not in applied:
                continue
            expected = candidate_values[item.parameter_id]
            result = backend.verify(item, expected, fencing_token=fencing_token)
            self._operation_event(events, SafetyState.VERIFY, result)
            if not result.succeeded:
                return self._rollback(
                    manifest,
                    backend,
                    snapshot,
                    applied,
                    fencing_token,
                    events,
                    reason=f"verify failed for {item.id}",
                )

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
            return self._rollback(
                manifest,
                backend,
                snapshot,
                applied,
                fencing_token,
                events,
                reason=f"measurement {measurement.status.value}",
            )

        if keep and self.policy.allow_keep and keep_authorized:
            final_snapshot = backend.snapshot(ordered, fencing_token=fencing_token)
            if not final_snapshot.complete:
                return self._rollback(
                    manifest,
                    backend,
                    snapshot,
                    applied,
                    fencing_token,
                    events,
                    reason="kept state could not be verified",
                )
            event(
                SafetyState.KEPT,
                "keep",
                "succeeded",
                round_trip_digest=final_snapshot.digest,
            )
            return SafetyResult(
                state=SafetyState.KEPT,
                events=tuple(events),
                snapshot=snapshot,
                final_snapshot=final_snapshot,
                applied_items=applied,
            )

        keep_reason = None
        if keep:
            keep_reason = "keep was requested without both policy and explicit authorization"
        return self._rollback(
            manifest,
            backend,
            snapshot,
            applied,
            fencing_token,
            events,
            reason=keep_reason or "default rollback after measurement",
        )

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
    ) -> SafetyResult:
        item_by_id = {item.id: item for item in manifest.items}
        rollback_failed = False
        for item_id in reversed(applied):
            item = item_by_id[item_id]
            snapshot_value = snapshot.entries[item_id].value
            result = backend.rollback(item, snapshot_value, fencing_token=fencing_token)
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
            verify = backend.verify(item, snapshot_value, fencing_token=fencing_token)
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
        final_snapshot = backend.snapshot(ordered, fencing_token=fencing_token)
        round_trip_matches = final_snapshot.complete and final_snapshot.digest == snapshot.digest
        if not round_trip_matches:
            rollback_failed = True
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
        state = SafetyState.NEEDS_ATTENTION if rollback_failed else SafetyState.ROLLED_BACK
        events.append(
            SafetyEvent(
                sequence=len(events),
                state=state,
                operation="rollback-complete",
                status="failed" if rollback_failed else "succeeded",
                reason=reason,
                snapshot_digest=snapshot.digest,
                round_trip_digest=final_snapshot.digest,
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
    "SafetyController",
    "SafetyEvent",
    "SafetyPolicy",
    "SafetyResult",
    "SafetyState",
]
