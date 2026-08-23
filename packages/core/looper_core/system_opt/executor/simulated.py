from __future__ import annotations

from typing import Any

from pydantic import Field

from looper_core.canonical import canonical_json
from looper_core.contracts import StrictModel
from looper_core.system_opt.config_manifest import ConfigCategory, ConfigItem
from looper_core.system_opt.executor import (
    BackendCapabilities,
    BackendKind,
    ConfigSnapshot,
    OperationResult,
    OperationStatus,
    PreflightCheckResult,
    ProbeResult,
    SnapshotEntry,
)


def _condition_matches(actual: Any, operator: str, expected: Any) -> bool:
    if operator == "exists":
        return actual is not None and actual is not False
    if operator == "eq":
        return canonical_json(actual) == canonical_json(expected)
    if operator in {"gt", "gte"}:
        if isinstance(actual, bool) or isinstance(expected, bool):
            return False
        try:
            return actual > expected if operator == "gt" else actual >= expected
        except TypeError:
            return False
    if operator == "in":
        return isinstance(expected, list) and any(
            canonical_json(actual) == canonical_json(value) for value in expected
        )
    return False


class SimulatedFailurePlan(StrictModel):
    unavailable_items: set[str] = Field(default_factory=set)
    apply_failures: set[str] = Field(default_factory=set)
    partial_apply_failures: set[str] = Field(default_factory=set)
    verify_failures: set[str] = Field(default_factory=set)
    rollback_failures: set[str] = Field(default_factory=set)
    drift_on_verify: dict[str, Any] = Field(default_factory=dict)
    virtual_delays: dict[str, float] = Field(default_factory=dict)


class SimulatedBackend:
    def __init__(
        self,
        initial_state: dict[str, Any],
        *,
        target_id: str = "simulated-target",
        seed: int = 0,
        failure_plan: SimulatedFailurePlan | None = None,
        precondition_facts: dict[str, Any] | None = None,
    ) -> None:
        self._state = dict(initial_state)
        self._seed = seed
        self._plan = failure_plan or SimulatedFailurePlan()
        self._latest_fencing_token = -1
        self._drift_consumed: set[str] = set()
        self._precondition_facts = dict(precondition_facts or {})
        self.operations: list[OperationResult] = []
        self._capabilities = BackendCapabilities(
            kind=BackendKind.SIMULATED,
            target_id=target_id,
            os="simulated",
            enabled=True,
            privileged=True,
            categories=set(ConfigCategory),
            supports_fencing=True,
            out_of_band_recovery=True,
        )

    @property
    def capabilities(self) -> BackendCapabilities:
        return self._capabilities

    @property
    def seed(self) -> int:
        return self._seed

    def state(self) -> dict[str, Any]:
        return dict(self._state)

    def inject_drift(self, item_id: str, value: Any) -> None:
        self._state[item_id] = value

    def preflight_check(self, item: ConfigItem) -> PreflightCheckResult:
        for condition in item.preconditions:
            key = f"{condition.kind}:{condition.key}"
            if key not in self._precondition_facts:
                return PreflightCheckResult(
                    item_id=item.id,
                    status=OperationStatus.UNAVAILABLE,
                    message=f"precondition fact {key!r} was not provided",
                )
            actual = self._precondition_facts[key]
            if not _condition_matches(actual, condition.operator, condition.value):
                return PreflightCheckResult(
                    item_id=item.id,
                    status=OperationStatus.FAILED,
                    message=f"precondition {key!r} did not match",
                )
        return PreflightCheckResult(item_id=item.id, status=OperationStatus.SUCCEEDED)

    def _delay(self, operation: str, item_id: str) -> float:
        return float(self._plan.virtual_delays.get(f"{operation}:{item_id}", 0.0))

    def _fence_error(self, item_id: str, operation: str, fencing_token: int) -> str | None:
        if fencing_token < 0:
            return "fencing token must be non-negative"
        if fencing_token < self._latest_fencing_token:
            return f"stale fencing token {fencing_token}; latest is {self._latest_fencing_token}"
        self._latest_fencing_token = max(self._latest_fencing_token, fencing_token)
        return None

    def probe(self, item: ConfigItem, *, fencing_token: int) -> ProbeResult:
        fence_error = self._fence_error(item.id, "probe", fencing_token)
        if fence_error:
            return ProbeResult(item_id=item.id, status=OperationStatus.FAILED, message=fence_error)
        delay = self._delay("probe", item.id)
        if item.id in self._plan.unavailable_items or item.id not in self._state:
            return ProbeResult(
                item_id=item.id,
                status=OperationStatus.UNAVAILABLE,
                message="simulated item is unavailable",
                virtual_elapsed_seconds=delay,
            )
        return ProbeResult(
            item_id=item.id,
            status=OperationStatus.SUCCEEDED,
            value=self._state[item.id],
            raw_output=canonical_json(self._state[item.id]),
            virtual_elapsed_seconds=delay,
        )

    def snapshot(self, items: list[ConfigItem], *, fencing_token: int) -> ConfigSnapshot:
        entries: dict[str, SnapshotEntry] = {}
        for item in sorted(items, key=lambda candidate: candidate.id):
            probe = self.probe(item, fencing_token=fencing_token)
            entries[item.id] = SnapshotEntry(
                item_id=item.id,
                target=item.target,
                status=probe.status,
                value=probe.value,
                message=probe.message,
                raw_output=probe.raw_output,
            )
        return ConfigSnapshot(target_id=self.capabilities.target_id, entries=entries)

    def apply(self, item: ConfigItem, value: Any, *, fencing_token: int) -> OperationResult:
        fence_error = self._fence_error(item.id, "apply", fencing_token)
        old_value = self._state.get(item.id)
        delay = self._delay("apply", item.id)
        if fence_error:
            result = OperationResult(
                operation="apply",
                item_id=item.id,
                status=OperationStatus.FAILED,
                old_value=old_value,
                requested_value=value,
                message=fence_error,
                virtual_elapsed_seconds=delay,
            )
        elif item.id in self._plan.partial_apply_failures:
            self._state[item.id] = value
            result = OperationResult(
                operation="apply",
                item_id=item.id,
                status=OperationStatus.UNKNOWN,
                old_value=old_value,
                requested_value=value,
                message="injected unknown result after partial apply",
                virtual_elapsed_seconds=delay,
            )
        elif item.id in self._plan.apply_failures:
            result = OperationResult(
                operation="apply",
                item_id=item.id,
                status=OperationStatus.FAILED,
                old_value=old_value,
                requested_value=value,
                message="injected apply failure",
                virtual_elapsed_seconds=delay,
            )
        elif item.id not in self._state:
            result = OperationResult(
                operation="apply",
                item_id=item.id,
                status=OperationStatus.UNAVAILABLE,
                old_value=None,
                requested_value=value,
                message="simulated item is unavailable",
                virtual_elapsed_seconds=delay,
            )
        else:
            try:
                item.validate_value(value)
            except ValueError as error:
                result = OperationResult(
                    operation="apply",
                    item_id=item.id,
                    status=OperationStatus.FAILED,
                    old_value=old_value,
                    requested_value=value,
                    message=str(error),
                    virtual_elapsed_seconds=delay,
                )
            else:
                self._state[item.id] = value
                result = OperationResult(
                    operation="apply",
                    item_id=item.id,
                    status=OperationStatus.SUCCEEDED,
                    old_value=old_value,
                    requested_value=value,
                    readback_value=value,
                    virtual_elapsed_seconds=delay,
                )
        self.operations.append(result)
        return result

    def verify(self, item: ConfigItem, expected: Any, *, fencing_token: int) -> OperationResult:
        fence_error = self._fence_error(item.id, "verify", fencing_token)
        delay = self._delay("verify", item.id)
        if fence_error:
            result = OperationResult(
                operation="verify",
                item_id=item.id,
                status=OperationStatus.FAILED,
                requested_value=expected,
                message=fence_error,
                virtual_elapsed_seconds=delay,
            )
        else:
            if item.id in self._plan.drift_on_verify and item.id not in self._drift_consumed:
                self._state[item.id] = self._plan.drift_on_verify[item.id]
                self._drift_consumed.add(item.id)
            actual = self._state.get(item.id)
            matches = canonical_json(actual) == canonical_json(expected)
            injected_failure = item.id in self._plan.verify_failures
            result = OperationResult(
                operation="verify",
                item_id=item.id,
                status=(
                    OperationStatus.SUCCEEDED
                    if matches and not injected_failure
                    else OperationStatus.FAILED
                ),
                requested_value=expected,
                readback_value=actual,
                message=(
                    None
                    if matches and not injected_failure
                    else "injected verify failure"
                    if injected_failure
                    else "readback does not match expected value"
                ),
                virtual_elapsed_seconds=delay,
            )
        self.operations.append(result)
        return result

    def rollback(
        self, item: ConfigItem, snapshot_value: Any, *, fencing_token: int
    ) -> OperationResult:
        fence_error = self._fence_error(item.id, "rollback", fencing_token)
        current = self._state.get(item.id)
        delay = self._delay("rollback", item.id)
        if fence_error:
            result = OperationResult(
                operation="rollback",
                item_id=item.id,
                status=OperationStatus.FAILED,
                old_value=current,
                requested_value=snapshot_value,
                message=fence_error,
                virtual_elapsed_seconds=delay,
            )
        elif item.id in self._plan.rollback_failures:
            result = OperationResult(
                operation="rollback",
                item_id=item.id,
                status=OperationStatus.FAILED,
                old_value=current,
                requested_value=snapshot_value,
                message="injected rollback failure",
                virtual_elapsed_seconds=delay,
            )
        else:
            self._state[item.id] = snapshot_value
            result = OperationResult(
                operation="rollback",
                item_id=item.id,
                status=OperationStatus.SUCCEEDED,
                old_value=current,
                requested_value=snapshot_value,
                readback_value=snapshot_value,
                virtual_elapsed_seconds=delay,
            )
        self.operations.append(result)
        return result


__all__ = ["SimulatedBackend", "SimulatedFailurePlan"]
