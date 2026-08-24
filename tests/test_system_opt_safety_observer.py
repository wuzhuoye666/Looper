from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from looper_core.system_opt.config_manifest import ConfigItem
from looper_core.system_opt.executor import (
    BackendCapabilities,
    ConfigSnapshot,
    OperationResult,
    PreflightCheckResult,
)
from looper_core.system_opt.executor.simulated import SimulatedBackend
from looper_core.system_opt.safety import (
    MeasurementResult,
    MeasurementStatus,
    ProgressRecordError,
    SafetyController,
    SafetyPolicy,
    SafetyProgressEvent,
    SafetyProgressStage,
    SafetyState,
)
from system_opt_support import integer_item, manifest


class RaisingBackend:
    def __init__(
        self,
        initial: dict[str, Any],
        *,
        raise_capabilities: bool = False,
        raise_preflight: bool = False,
        raise_apply: bool = False,
        raise_rollback: bool = False,
        raise_verify_calls: set[int] | None = None,
        raise_snapshot_calls: set[int] | None = None,
    ) -> None:
        self.delegate = SimulatedBackend(initial)
        self.raise_capabilities = raise_capabilities
        self.raise_preflight = raise_preflight
        self.raise_apply = raise_apply
        self.raise_rollback = raise_rollback
        self.raise_verify_calls = set(raise_verify_calls or set())
        self.raise_snapshot_calls = set(raise_snapshot_calls or set())
        self.verify_calls = 0
        self.snapshot_calls = 0

    @property
    def capabilities(self) -> BackendCapabilities:
        if self.raise_capabilities:
            raise RuntimeError("capabilities exploded")
        return self.delegate.capabilities

    def preflight_check(self, item: ConfigItem) -> PreflightCheckResult:
        if self.raise_preflight:
            raise RuntimeError("preflight exploded")
        return self.delegate.preflight_check(item)

    def snapshot(
        self, items: list[ConfigItem], *, fencing_token: int
    ) -> ConfigSnapshot:
        self.snapshot_calls += 1
        if self.snapshot_calls in self.raise_snapshot_calls:
            raise RuntimeError(f"snapshot {self.snapshot_calls} exploded")
        return self.delegate.snapshot(items, fencing_token=fencing_token)

    def apply(
        self, item: ConfigItem, value: Any, *, fencing_token: int
    ) -> OperationResult:
        result = self.delegate.apply(item, value, fencing_token=fencing_token)
        if self.raise_apply:
            raise RuntimeError("apply exploded after write")
        return result

    def verify(
        self, item: ConfigItem, expected: Any, *, fencing_token: int
    ) -> OperationResult:
        self.verify_calls += 1
        if self.verify_calls in self.raise_verify_calls:
            raise RuntimeError(f"verify {self.verify_calls} exploded")
        return self.delegate.verify(item, expected, fencing_token=fencing_token)

    def rollback(
        self, item: ConfigItem, snapshot_value: Any, *, fencing_token: int
    ) -> OperationResult:
        result = self.delegate.rollback(item, snapshot_value, fencing_token=fencing_token)
        if self.raise_rollback:
            raise RuntimeError("rollback exploded after write")
        return result


def _controller() -> SafetyController:
    return SafetyController(SafetyPolicy(allow_keep=True))


@pytest.mark.parametrize("seam", ["capabilities", "preflight", "snapshot"])
def test_backend_exception_before_apply_is_structured_rejection(seam: str) -> None:
    item = integer_item()
    backend = RaisingBackend(
        {item.id: 60},
        raise_capabilities=seam == "capabilities",
        raise_preflight=seam == "preflight",
        raise_snapshot_calls={1} if seam == "snapshot" else set(),
    )

    result = _controller().execute(
        manifest(item), {item.parameter_id: 10}, backend, fencing_token=1
    )

    assert result.state is SafetyState.REJECTED
    assert backend.delegate.state()[item.id] == 60
    assert not any(operation.operation == "apply" for operation in backend.delegate.operations)
    assert "RuntimeError" in str(result.reason)


@pytest.mark.parametrize(
    ("backend_factory", "expected_state"),
    [
        (lambda item: RaisingBackend({item.id: 60}, raise_apply=True), SafetyState.ROLLED_BACK),
        (
            lambda item: RaisingBackend({item.id: 60}, raise_verify_calls={1}),
            SafetyState.ROLLED_BACK,
        ),
        (
            lambda item: RaisingBackend({item.id: 60}, raise_rollback=True),
            SafetyState.NEEDS_ATTENTION,
        ),
        (
            lambda item: RaisingBackend({item.id: 60}, raise_verify_calls={2}),
            SafetyState.NEEDS_ATTENTION,
        ),
        (
            lambda item: RaisingBackend({item.id: 60}, raise_snapshot_calls={2}),
            SafetyState.NEEDS_ATTENTION,
        ),
    ],
)
def test_backend_exception_after_apply_is_compensated(
    backend_factory: Callable[[ConfigItem], RaisingBackend], expected_state: SafetyState
) -> None:
    item = integer_item()
    backend = backend_factory(item)

    result = _controller().execute(
        manifest(item),
        {item.parameter_id: 10},
        backend,
        fencing_token=2,
        measure=lambda: MeasurementResult(
            status=MeasurementStatus.FAILED, reason="force rollback"
        ),
    )

    assert result.state is expected_state
    assert result.applied_items == [item.id]
    assert any("RuntimeError" in str(event.reason) for event in result.events)


def test_rollback_exception_continues_compensating_remaining_items() -> None:
    first = integer_item("first", target="vm.first")
    second = integer_item("second", target="vm.second", dependencies=["first"])
    backend = RaisingBackend(
        {first.id: 60, second.id: 60},
        raise_rollback=True,
    )

    result = _controller().execute(
        manifest(first, second),
        {first.parameter_id: 10, second.parameter_id: 20},
        backend,
        fencing_token=20,
        measure=lambda: MeasurementResult(status=MeasurementStatus.FAILED),
    )

    rollback_ids = [
        operation.item_id
        for operation in backend.delegate.operations
        if operation.operation == "rollback"
    ]
    assert result.state is SafetyState.NEEDS_ATTENTION
    assert rollback_ids == [second.id, first.id]
    assert backend.delegate.state() == {first.id: 60, second.id: 60}


def test_keep_snapshot_exception_routes_through_rollback() -> None:
    item = integer_item()
    backend = RaisingBackend({item.id: 60}, raise_snapshot_calls={2})

    result = _controller().execute(
        manifest(item),
        {item.parameter_id: 10},
        backend,
        fencing_token=3,
        keep=True,
        keep_authorized=True,
    )

    assert result.state is SafetyState.ROLLED_BACK
    assert backend.delegate.state()[item.id] == 60
    assert backend.snapshot_calls == 3


def test_observer_receives_terminal_digest_after_result_is_complete() -> None:
    item = integer_item()
    backend = RaisingBackend({item.id: 60})
    progress: list[SafetyProgressEvent] = []

    observed = _controller().execute_observed(
        manifest(item),
        {item.parameter_id: 10},
        backend,
        fencing_token=4,
        keep=True,
        keep_authorized=True,
        progress_observer=progress.append,
    )

    assert [event.stage for event in progress] == [
        SafetyProgressStage.PREFLIGHT_COMPLETED,
        SafetyProgressStage.APPLY_STARTED,
        SafetyProgressStage.SAFETY_TERMINAL,
    ]
    assert progress[-1].evidence_digest == observed.result.digest
    assert observed.progress_failures == []
    assert set(observed.result.model_dump()) == {
        "state",
        "events",
        "snapshot",
        "final_snapshot",
        "applied_items",
        "reason",
    }


def test_observer_failure_before_apply_preserves_cause_and_prevents_write() -> None:
    item = integer_item()
    backend = RaisingBackend({item.id: 60})

    def observer(event: SafetyProgressEvent) -> None:
        if event.stage is SafetyProgressStage.APPLY_STARTED:
            raise OSError("receipt unavailable")

    with pytest.raises(ProgressRecordError) as raised:
        _controller().execute_observed(
            manifest(item),
            {item.parameter_id: 10},
            backend,
            fencing_token=5,
            progress_observer=observer,
        )

    assert isinstance(raised.value.__cause__, OSError)
    assert backend.delegate.state()[item.id] == 60
    assert not any(operation.operation == "apply" for operation in backend.delegate.operations)


def test_observer_failure_after_apply_taints_later_receipts_but_not_compensation() -> None:
    item = integer_item()
    backend = RaisingBackend({item.id: 60})

    def observer(event: SafetyProgressEvent) -> None:
        if event.stage is SafetyProgressStage.ROLLBACK_STARTED:
            raise OSError("receipt unavailable after apply")

    observed = _controller().execute_observed(
        manifest(item),
        {item.parameter_id: 10},
        backend,
        fencing_token=6,
        measure=lambda: MeasurementResult(status=MeasurementStatus.FAILED),
        progress_observer=observer,
    )

    assert observed.result.state is SafetyState.ROLLED_BACK
    assert backend.delegate.state()[item.id] == 60
    assert [failure.stage for failure in observed.progress_failures] == [
        SafetyProgressStage.ROLLBACK_STARTED,
        SafetyProgressStage.ROLLBACK_VERIFIED,
        SafetyProgressStage.SAFETY_TERMINAL,
    ]
    assert observed.progress_failures[0].error_type == "OSError"
    assert all(
        failure.error_type == "ProgressChannelTainted"
        for failure in observed.progress_failures[1:]
    )
