from __future__ import annotations

import pytest
from looper_core.analysis import InsufficientEvidence
from looper_core.contracts import Operator
from looper_core.system_opt.demo import (
    SyntheticMeasurementAdapter,
    build_demo_manifest,
    build_demo_policy,
    build_workload_reference,
)
from looper_core.system_opt.executor.simulated import SimulatedBackend
from looper_core.system_opt.policy import (
    HardGateContract,
    MetricContract,
    MetricDirection,
    OptimizationMode,
    PressureMethod,
    SystemOptimizationPolicy,
)
from looper_core.system_opt.scoring import (
    DiagnosticPriority,
    MeasurementBatch,
    MetricEvidence,
    adverse_change,
    bootstrap_improvement,
    comparable,
    diagnostic_evidence_report,
    diagnostic_priorities,
    evaluate_hard_gates,
    improvement_value,
    pressure_value,
)
from pydantic import ValidationError


def test_policy_has_no_implicit_search_or_statistics_contract() -> None:
    payload = build_demo_policy(OptimizationMode.GENERAL).model_dump(mode="python")
    payload.pop("statistics")

    with pytest.raises(ValidationError, match="statistics"):
        SystemOptimizationPolicy.model_validate(payload)


def test_periodic_baseline_interval_must_be_explicit() -> None:
    payload = build_demo_policy(OptimizationMode.GENERAL).model_dump(mode="python")
    payload["statistics"].pop("baseline_every_n")

    with pytest.raises(ValidationError, match="baseline_every_n"):
        SystemOptimizationPolicy.model_validate(payload)


def test_identity_and_missing_gate_fail_closed() -> None:
    matches, mismatches = comparable(
        {"target": "a", "phase": "steady"},
        {"target": "a"},
        ["target", "phase"],
    )
    gate = HardGateContract(
        id="correctness",
        metric="gate.correctness",
        operator=Operator.TRUE,
        threshold=None,
        reason="correctness is mandatory",
    )

    assert not matches
    assert mismatches == ["phase"]
    assert not evaluate_hard_gates([gate], {})[0].passed


def test_directional_improvement_and_confidence_lower_bound() -> None:
    manifest = build_demo_manifest()
    backend = SimulatedBackend({item.id: item.default for item in manifest.items})
    policy = build_demo_policy(OptimizationMode.GENERAL)
    adapter = SyntheticMeasurementAdapter(backend, mode=OptimizationMode.GENERAL)
    baseline = adapter(policy.statistics.baseline_repeats)
    backend.inject_drift("cpu-governor", "performance")
    candidate = adapter(policy.statistics.candidate_repeats)
    contract = policy.primary_metric

    evidence = bootstrap_improvement(
        candidate.metrics[contract.id],
        baseline.metrics[contract.id],
        contract,
        policy.statistics,
    )

    assert evidence.estimate > 0
    assert evidence.lower > evidence.minimum_effect
    assert evidence.accepted


def test_workload_priority_routes_high_pressure_adverse_cpu() -> None:
    manifest = build_demo_manifest()
    backend = SimulatedBackend({item.id: item.default for item in manifest.items})
    policy = build_demo_policy(OptimizationMode.WORKLOAD)
    current = SyntheticMeasurementAdapter(backend, mode=OptimizationMode.WORKLOAD)(7)
    reference = build_workload_reference(policy)
    contracts = [metric for metric in policy.metrics if metric.role.value == "component-diagnostic"]

    priorities = diagnostic_priorities(current, reference, contracts)

    assert priorities[0].component == "cpu"
    assert priorities[0].pressure == pytest.approx(0.92)
    assert priorities[0].adverse_change > 0
    assert priorities[0].formula_id == "F-PROJECT-S4-PIECEWISE-LINEAR/v1alpha1"
    assert priorities[0].current_batch_digest == current.digest
    assert priorities[0].reference_batch_digest == reference.digest


def test_utilization_change_uses_capacity_reference_not_baseline_or_scale() -> None:
    contract = build_demo_policy(OptimizationMode.WORKLOAD).metric("cpu.utilization")
    payload = contract.model_dump(mode="python")
    payload["scale"] = None
    unscaled = contract.model_construct(**payload)

    assert adverse_change(0.2, 0.0, unscaled) == pytest.approx(0.2)
    assert adverse_change(0.2001, 0.0001, unscaled) == pytest.approx(0.2)


def _metric_with(
    *,
    direction: MetricDirection,
    scale: float | None,
    target: float | None = None,
    lower_bound: float | None = None,
    upper_bound: float | None = None,
    pressure_method: PressureMethod = PressureMethod.NONE,
    pressure_reference: float | None = None,
) -> MetricContract:
    payload = build_demo_policy(OptimizationMode.GENERAL).metric(
        "workload.score"
    ).model_dump(mode="python")
    payload.update(
        direction=direction,
        scale=scale,
        target=target,
        lower_bound=lower_bound,
        upper_bound=upper_bound,
        pressure_method=pressure_method,
        pressure_reference=pressure_reference,
    )
    return MetricContract.model_construct(**payload)


def test_target_improvement_requires_explicit_scale() -> None:
    contract = _metric_with(direction=MetricDirection.TARGET, scale=None, target=100.0)
    with pytest.raises(InsufficientEvidence, match="explicit scale"):
        improvement_value(120.0, 100.0, contract)


def test_target_adverse_change_requires_explicit_scale() -> None:
    contract = _metric_with(direction=MetricDirection.TARGET, scale=None, target=100.0)
    with pytest.raises(InsufficientEvidence, match="explicit scale"):
        adverse_change(120.0, 100.0, contract)


def test_excess_pressure_requires_explicit_scale() -> None:
    contract = _metric_with(
        direction=MetricDirection.MINIMIZE,
        scale=None,
        pressure_method=PressureMethod.UPPER_LIMIT_EXCESS,
        pressure_reference=0.5,
    )
    with pytest.raises(InsufficientEvidence, match="explicit scale"):
        pressure_value(0.9, contract)


def test_negative_utilization_fails_closed() -> None:
    contract = build_demo_policy(OptimizationMode.WORKLOAD).metric("cpu.utilization")
    with pytest.raises(InsufficientEvidence, match="negative utilization"):
        pressure_value(-0.1, contract)


@pytest.mark.parametrize(
    "direction,bounds",
    [
        (MetricDirection.TARGET, {}),
        (
            MetricDirection.RANGE,
            {"lower_bound": 90.0, "upper_bound": 110.0},
        ),
    ],
)
def test_target_range_adverse_change_is_negated_improvement(
    direction: MetricDirection, bounds: dict[str, float]
) -> None:
    """SO-D023 M1: after scale normalization the S4 adverse change for
    target/range metrics is exactly the negated S6 improvement — the identity
    that keeps the two formulas one coordinate system, pinned as a property."""

    contract = _metric_with(direction=direction, scale=2.0, target=100.0, **bounds)
    for current, baseline in ((105.0, 100.0), (118.0, 95.0), (99.0, 130.0)):
        assert adverse_change(current, baseline, contract) == pytest.approx(
            -improvement_value(current, baseline, contract)
        ), f"identity broken for current={current}, baseline={baseline}"

def test_f_project_002_adverse_change_is_continuous_across_zero() -> None:
    contract = _metric_with(
        direction=MetricDirection.MINIMIZE,
        scale=0.1,
        pressure_method=PressureMethod.UPPER_LIMIT_EXCESS,
        pressure_reference=0.5,
    )

    assert adverse_change(0.0002, 0.0001, contract) == pytest.approx(0.001)
    assert adverse_change(0.0001, 0.0, contract) == pytest.approx(0.001)


@pytest.mark.parametrize(
    "method,direction,current,baseline,reference,scale,expected",
    [
        (PressureMethod.UTILIZATION, MetricDirection.DIAGNOSTIC_ONLY, 0.8, 0.6, 1.0, 7.0, 0.2),
        (PressureMethod.UPPER_LIMIT_EXCESS, MetricDirection.MINIMIZE, 12.0, 10.0, 9.0, 4.0, 0.5),
        (PressureMethod.LOWER_LIMIT_DEFICIT, MetricDirection.MAXIMIZE, 8.0, 10.0, 9.0, 4.0, 0.5),
        (PressureMethod.TARGET_DISTANCE, MetricDirection.TARGET, 14.0, 11.0, 10.0, 2.0, 1.5),
        (PressureMethod.RANGE_EXCESS, MetricDirection.RANGE, 14.0, 11.0, None, 2.0, 1.5),
        (PressureMethod.EXPLICIT_SCORE, MetricDirection.DIAGNOSTIC_ONLY, 0.8, 0.3, None, 9.0, 0.5),
    ],
)
def test_f_project_002_adverse_transform_family(
    method: PressureMethod,
    direction: MetricDirection,
    current: float,
    baseline: float,
    reference: float | None,
    scale: float,
    expected: float,
) -> None:
    bounds = {"lower_bound": 9.0, "upper_bound": 11.0} if direction == MetricDirection.RANGE else {}
    contract = _metric_with(
        direction=direction,
        scale=scale,
        target=reference if direction == MetricDirection.TARGET else None,
        pressure_method=method,
        pressure_reference=reference,
        **bounds,
    )

    assert adverse_change(current, baseline, contract) == pytest.approx(expected)


@pytest.mark.parametrize(
    "method,direction",
    [
        (PressureMethod.UPPER_LIMIT_EXCESS, MetricDirection.MAXIMIZE),
        (PressureMethod.LOWER_LIMIT_DEFICIT, MetricDirection.MINIMIZE),
        (PressureMethod.TARGET_DISTANCE, MetricDirection.MINIMIZE),
        (PressureMethod.RANGE_EXCESS, MetricDirection.TARGET),
        (PressureMethod.EXPLICIT_SCORE, MetricDirection.MAXIMIZE),
    ],
)
def test_pressure_method_rejects_incompatible_direction(
    method: PressureMethod, direction: MetricDirection
) -> None:
    payload = build_demo_policy(OptimizationMode.GENERAL).metric("workload.score").model_dump(
        mode="python"
    )
    payload.update(
        role="component-diagnostic",
        direction=direction,
        scale=1.0,
        target=0.5 if direction == MetricDirection.TARGET else None,
        lower_bound=0.0 if direction == MetricDirection.RANGE else None,
        upper_bound=1.0 if direction == MetricDirection.RANGE else None,
        pressure_method=method,
        pressure_reference=None if method == PressureMethod.EXPLICIT_SCORE else 0.5,
    )

    with pytest.raises(ValidationError, match="incompatible"):
        MetricContract.model_validate(payload)


def test_target_pressure_reference_must_equal_target() -> None:
    payload = build_demo_policy(OptimizationMode.GENERAL).metric("workload.score").model_dump(
        mode="python"
    )
    payload.update(
        role="component-diagnostic",
        direction=MetricDirection.TARGET,
        target=10.0,
        scale=2.0,
        pressure_method=PressureMethod.TARGET_DISTANCE,
        pressure_reference=11.0,
    )

    with pytest.raises(ValidationError, match="must equal metric target"):
        MetricContract.model_validate(payload)


def test_idle_batch_uses_measurement_model_but_cannot_enter_diagnosis() -> None:
    manifest = build_demo_manifest()
    backend = SimulatedBackend({item.id: item.default for item in manifest.items})
    policy = build_demo_policy(OptimizationMode.WORKLOAD)
    loaded = SyntheticMeasurementAdapter(backend, mode=OptimizationMode.WORKLOAD)(7)
    idle_payload = loaded.model_dump(mode="python")
    idle_payload["identity"]["load_state"] = "idle"
    idle = type(loaded).model_validate(idle_payload)
    contracts = [metric for metric in policy.metrics if metric.role.value == "component-diagnostic"]

    assert idle.identity["load_state"] == "idle"
    with pytest.raises(InsufficientEvidence, match="requires load_state=loaded"):
        diagnostic_priorities(idle, idle, contracts)


def test_loaded_diagnosis_rejects_cross_phase_reference() -> None:
    manifest = build_demo_manifest()
    backend = SimulatedBackend({item.id: item.default for item in manifest.items})
    policy = build_demo_policy(OptimizationMode.WORKLOAD)
    current = SyntheticMeasurementAdapter(backend, mode=OptimizationMode.WORKLOAD)(7)
    reference = build_workload_reference(policy)
    payload = reference.model_dump(mode="python")
    payload["identity"]["phase"] = "idle-baseline"
    cross_phase = type(reference).model_validate(payload)
    contracts = [metric for metric in policy.metrics if metric.role.value == "component-diagnostic"]

    with pytest.raises(InsufficientEvidence, match="identity mismatch.*phase"):
        diagnostic_priorities(current, cross_phase, contracts)


def test_component_pressure_can_worsen_while_business_utility_improves() -> None:
    policy = build_demo_policy(OptimizationMode.WORKLOAD)
    component = policy.metric("cpu.utilization")
    business = policy.metric("workload.score")

    component_change = adverse_change(0.9, 0.7, component)
    business_change = improvement_value(110.0, 100.0, business)

    assert component_change > 0
    assert business_change > 0


def test_loaded_diagnosis_rejects_metric_contract_from_another_phase() -> None:
    manifest = build_demo_manifest()
    backend = SimulatedBackend({item.id: item.default for item in manifest.items})
    policy = build_demo_policy(OptimizationMode.WORKLOAD)
    current = SyntheticMeasurementAdapter(backend, mode=OptimizationMode.WORKLOAD)(7)
    reference = build_workload_reference(policy)
    contract = policy.metric("cpu.utilization")
    payload = contract.model_dump(mode="python")
    payload["phase"] = "idle-baseline"
    wrong_phase_contract = MetricContract.model_validate(payload)

    with pytest.raises(InsufficientEvidence, match="metric phase.*does not match"):
        diagnostic_priorities(current, reference, [wrong_phase_contract])

def _workload_diagnostic_fixture():
    manifest = build_demo_manifest()
    backend = SimulatedBackend({item.id: item.default for item in manifest.items})
    policy = build_demo_policy(OptimizationMode.WORKLOAD)
    current = SyntheticMeasurementAdapter(backend, mode=OptimizationMode.WORKLOAD)(7)
    reference = build_workload_reference(policy)
    contracts = [
        metric for metric in policy.metrics if metric.role.value == "component-diagnostic"
    ]
    return current, reference, contracts


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_metric_evidence_rejects_non_finite_values(value: float) -> None:
    with pytest.raises(ValidationError, match="must be finite"):
        MetricEvidence(metric_id="cpu.utilization", values=[value])


def test_measurement_batch_rejects_metric_dictionary_key_mismatch() -> None:
    with pytest.raises(ValidationError, match="metric key/id mismatch"):
        MeasurementBatch(
            identity={},
            metrics={"cpu.utilization": MetricEvidence(metric_id="wrong.metric", values=[0.5])},
            gate_values={},
        )


@pytest.mark.parametrize("field", ["scale", "target", "pressure_reference"])
def test_metric_contract_rejects_non_finite_formula_parameters(field: str) -> None:
    contract = build_demo_policy(OptimizationMode.WORKLOAD).metric("cpu.utilization")
    payload = contract.model_dump(mode="python")
    payload[field] = float("inf")

    with pytest.raises(ValidationError, match="must be finite"):
        MetricContract.model_validate(payload)


def test_diagnosis_fails_closed_and_reports_insufficient_samples() -> None:
    current, reference, contracts = _workload_diagnostic_fixture()
    payload = current.model_dump(mode="python")
    metric_id = contracts[0].id
    payload["metrics"][metric_id]["values"] = [payload["metrics"][metric_id]["values"][0]]
    short_current = MeasurementBatch.model_validate(payload)

    report = diagnostic_evidence_report(short_current, reference, contracts)
    issue = next(item for item in report.issues if item.metric_id == metric_id)
    assert issue.reason == "insufficient-samples"
    assert issue.observed_samples == 1
    assert issue.required_samples == contracts[0].minimum_samples
    assert report.coverage < 1
    with pytest.raises(InsufficientEvidence, match="insufficient-samples"):
        diagnostic_priorities(short_current, reference, contracts)


def test_diagnosis_fails_closed_on_missing_or_mismatched_pressure_protocol() -> None:
    current, reference, contracts = _workload_diagnostic_fixture()
    missing_payload = current.model_dump(mode="python")
    missing_payload["pressure_protocol_digest"] = None
    missing = MeasurementBatch.model_validate(missing_payload)
    with pytest.raises(InsufficientEvidence, match="missing-pressure-protocol"):
        diagnostic_priorities(missing, reference, contracts)

    mismatch_payload = reference.model_dump(mode="python")
    mismatch_payload["pressure_protocol_digest"] = "sha256:" + "a" * 64
    mismatch = MeasurementBatch.model_validate(mismatch_payload)
    with pytest.raises(InsufficientEvidence, match="pressure-protocol-mismatch"):
        diagnostic_priorities(current, mismatch, contracts)


def test_missing_metric_is_explicit_in_diagnostic_coverage() -> None:
    current, reference, contracts = _workload_diagnostic_fixture()
    payload = reference.model_dump(mode="python")
    missing_metric = contracts[-1].id
    del payload["metrics"][missing_metric]
    incomplete = MeasurementBatch.model_validate(payload)

    report = diagnostic_evidence_report(current, incomplete, contracts)
    assert report.coverage == pytest.approx((len(contracts) - 1) / len(contracts))
    assert any(
        issue.metric_id == missing_metric
        and issue.side == "reference"
        and issue.reason == "missing-metric"
        for issue in report.issues
    )
    with pytest.raises(InsufficientEvidence, match="missing-metric"):
        diagnostic_priorities(current, incomplete, contracts)


def test_diagnostic_priority_keeps_legacy_sample_adequacy_schema() -> None:
    current, reference, contracts = _workload_diagnostic_fixture()
    priorities = diagnostic_priorities(current, reference, contracts)

    # M9 compatibility: serialized confidence still carries sample adequacy until
    # the P/D/A/Q/T DiagnosticPriority schema migration lands as one versioned event.
    assert all(priority.confidence == 1 for priority in priorities)
    # One diagnostic metric per component in the fixture: no component can dominate another.
    assert all(priority.pareto_rank == 1 for priority in priorities)


def test_legacy_diagnostic_confidence_still_loads() -> None:
    priority = DiagnosticPriority.model_validate(
        {
            "metric_id": "cpu.utilization",
            "component": "cpu",
            "pressure": 0.9,
            "adverse_change": 0.1,
            "persistence": 0.5,
            "confidence": 0.5,
        }
    )

    assert priority.confidence == 0.5
    assert set(priority.model_dump(mode="json")) == {
        "metric_id",
        "component",
        "pressure",
        "adverse_change",
        "persistence",
        "confidence",
        "pareto_rank",
        "formula_id",
        "current_batch_digest",
        "reference_batch_digest",
    }


def test_pareto_ranks_are_independent_between_components() -> None:
    current, reference, contracts = _workload_diagnostic_fixture()
    cpu = contracts[0]
    weaker_cpu = cpu.model_copy(update={"id": "cpu.utilization-secondary"})
    storage = contracts[2]
    current_payload = current.model_dump(mode="python")
    reference_payload = reference.model_dump(mode="python")
    current_payload["metrics"][weaker_cpu.id] = {
        "metric_id": weaker_cpu.id,
        "values": [0.7] * cpu.minimum_samples,
    }
    reference_payload["metrics"][weaker_cpu.id] = {
        "metric_id": weaker_cpu.id,
        "values": [0.55] * cpu.minimum_samples,
    }

    priorities = diagnostic_priorities(
        MeasurementBatch.model_validate(current_payload),
        MeasurementBatch.model_validate(reference_payload),
        [cpu, weaker_cpu, storage],
    )
    by_metric = {priority.metric_id: priority for priority in priorities}

    assert by_metric[cpu.id].pareto_rank == 1
    assert by_metric[weaker_cpu.id].pareto_rank == 2
    # Storage is not demoted by CPU coordinates: its rank starts at one in its own component.
    assert by_metric[storage.id].pareto_rank == 1
