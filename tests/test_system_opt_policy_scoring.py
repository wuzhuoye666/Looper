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
    OptimizationMode,
    SystemOptimizationPolicy,
)
from looper_core.system_opt.scoring import (
    adverse_change,
    bootstrap_improvement,
    comparable,
    diagnostic_priorities,
    evaluate_hard_gates,
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


def test_near_zero_diagnostic_change_requires_explicit_scale() -> None:
    contract = build_demo_policy(OptimizationMode.WORKLOAD).metric("cpu.utilization")
    payload = contract.model_dump(mode="python")
    payload["scale"] = None
    unscaled = contract.model_construct(**payload)

    with pytest.raises(InsufficientEvidence, match="explicit scale"):
        adverse_change(0.2, 0, unscaled)
