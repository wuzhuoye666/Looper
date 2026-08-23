from __future__ import annotations

from looper_core.system_opt.config_manifest import (
    CONFIG_MANIFEST_SCHEMA,
    ConfigManifest,
    Precondition,
)
from looper_core.system_opt.executor.simulated import (
    SimulatedBackend,
    SimulatedFailurePlan,
)
from looper_core.system_opt.safety import (
    MeasurementResult,
    MeasurementStatus,
    SafetyController,
    SafetyPolicy,
    SafetyState,
)
from system_opt_support import integer_item, manifest


def test_blacklist_rejected_before_snapshot() -> None:
    item = integer_item()
    item.target = "kernel.panic_on_oops"
    # Bypass manifest registration deliberately to verify the runtime defense-in-depth guard.
    config = ConfigManifest.model_construct(
        schema_version=CONFIG_MANIFEST_SCHEMA,
        id="unsafe-test",
        version="1",
        description="Deliberately invalid runtime safety fixture.",
        items=[item],
        metadata={},
    )
    backend = SimulatedBackend({item.id: 60})

    result = SafetyController().execute(config, {item.parameter_id: 10}, backend, fencing_token=1)

    assert result.state == SafetyState.REJECTED
    assert "blacklist" in str(result.reason)
    assert backend.operations == []


def test_verify_failure_rolls_back_in_reverse_order() -> None:
    first = integer_item("first", target="vm.first")
    second = integer_item("second", target="vm.second", dependencies=["first"])
    config = manifest(first, second)
    backend = SimulatedBackend(
        {first.id: 60, second.id: 60},
        failure_plan=SimulatedFailurePlan(drift_on_verify={second.id: 30}),
    )

    result = SafetyController().execute(
        config,
        {first.parameter_id: 10, second.parameter_id: 20},
        backend,
        fencing_token=2,
    )

    rollback_ids = [
        operation.item_id for operation in backend.operations if operation.operation == "rollback"
    ]
    assert result.state == SafetyState.ROLLED_BACK
    assert rollback_ids == ["second", "first"]
    assert backend.state() == {"first": 60, "second": 60}


def test_timeout_rolls_back() -> None:
    item = integer_item()
    config = manifest(item)
    backend = SimulatedBackend({item.id: 60})

    result = SafetyController().execute(
        config,
        {item.parameter_id: 10},
        backend,
        fencing_token=3,
        measure=lambda: MeasurementResult(
            status=MeasurementStatus.TIMEOUT, reason="benchmark timed out"
        ),
    )

    assert result.state == SafetyState.ROLLED_BACK
    assert backend.state()[item.id] == 60
    assert any(event.status == "timeout" for event in result.events)


def test_pinned_and_unknown_ownership_are_untouched() -> None:
    item = integer_item()
    config = manifest(item)
    for policy in (
        SafetyPolicy(pinned_items={item.id}),
        SafetyPolicy(ownership_unknown_items={item.id}),
    ):
        backend = SimulatedBackend({item.id: 60})
        result = SafetyController(policy).execute(
            config, {item.parameter_id: 10}, backend, fencing_token=4
        )
        assert result.state == SafetyState.REJECTED
        assert backend.state()[item.id] == 60
        assert backend.operations == []


def test_rollback_failure_marks_target_needs_attention() -> None:
    item = integer_item()
    config = manifest(item)
    backend = SimulatedBackend(
        {item.id: 60},
        failure_plan=SimulatedFailurePlan(rollback_failures={item.id}),
    )

    result = SafetyController().execute(
        config,
        {item.parameter_id: 10},
        backend,
        fencing_token=5,
        measure=lambda: MeasurementResult(status=MeasurementStatus.FAILED, reason="bad result"),
    )

    assert result.state == SafetyState.NEEDS_ATTENTION
    assert result.needs_attention
    assert backend.state()[item.id] == 10
    assert any(event.operation == "rollback_failed" for event in result.events)


def test_default_change_limit_rejects_before_snapshot() -> None:
    items = [integer_item(f"item-{index}", target=f"vm.item_{index}") for index in range(6)]
    config = manifest(*items)
    backend = SimulatedBackend({item.id: 60 for item in items})

    result = SafetyController().execute(
        config,
        {item.parameter_id: 10 for item in items},
        backend,
        fencing_token=6,
    )

    assert result.state == SafetyState.REJECTED
    assert "limit is 5" in str(result.reason)
    assert backend.operations == []


def test_keep_without_policy_and_explicit_authorization_rolls_back() -> None:
    item = integer_item()
    config = manifest(item)
    backend = SimulatedBackend({item.id: 60})

    result = SafetyController().execute(
        config,
        {item.parameter_id: 10},
        backend,
        fencing_token=7,
        keep=True,
        keep_authorized=True,
    )

    assert result.state == SafetyState.ROLLED_BACK
    assert "without both policy" in str(result.reason)
    assert backend.state()[item.id] == 60


def test_precondition_is_evaluated_and_missing_fact_fails_closed() -> None:
    item = integer_item(
        preconditions=[Precondition(kind="fact", key="numa.node_count", operator="gte", value=2)]
    )
    config = manifest(item)

    missing = SafetyController().execute(
        config,
        {item.parameter_id: 10},
        SimulatedBackend({item.id: 60}),
        fencing_token=8,
    )
    mismatched = SafetyController().execute(
        config,
        {item.parameter_id: 10},
        SimulatedBackend({item.id: 60}, precondition_facts={"fact:numa.node_count": 1}),
        fencing_token=8,
    )

    assert missing.state == SafetyState.REJECTED
    assert "was not provided" in str(missing.reason)
    assert mismatched.state == SafetyState.REJECTED
    assert "did not match" in str(mismatched.reason)


def test_partially_applied_current_item_is_compensated() -> None:
    item = integer_item()
    config = manifest(item)
    backend = SimulatedBackend(
        {item.id: 60},
        failure_plan=SimulatedFailurePlan(partial_apply_failures={item.id}),
    )

    result = SafetyController().execute(
        config,
        {item.parameter_id: 10},
        backend,
        fencing_token=9,
    )

    assert result.state == SafetyState.ROLLED_BACK
    assert backend.state()[item.id] == 60
    assert result.applied_items == [item.id]
    assert any(operation.operation == "rollback" for operation in backend.operations)
