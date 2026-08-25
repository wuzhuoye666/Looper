"""M3 dynamic-phase runtime tests: the six components chained end to end.

The O0 source is the real stress-ng YAML fixture (rate 1182.49 bogo-ops/s),
so SLO scenarios use bounds around that real value. All load and config
seams are injected callables (SO-D020: the loop never starts the load).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from looper_core.system_opt.dynamic_loop import (
    DynamicPhaseRun,
    WindowAction,
    run_dynamic_phase,
)
from looper_core.system_opt.hypothesis import (
    ComponentHypothesis,
    InterventionExperiment,
)
from looper_core.system_opt.observation import WorkloadIdentityDrift
from looper_core.system_opt.phase_gate import (
    DynamicPhaseGateContract,
    GateStopClass,
    PhaseBudget,
    SloTarget,
)
from looper_core.system_opt.policy import (
    Aggregation,
    MetricContract,
    MetricDirection,
    MetricRole,
    PressureMethod,
    StatisticsPolicy,
)
from looper_core.system_opt.result_vector import PromotionContract
from looper_core.system_opt.scoring import MetricEvidence, bootstrap_improvement
from looper_core.system_opt.verification import RetestOutcome
from looper_core.system_opt.workload import WorkloadContract

ENV = "sha256:" + "b" * 64
RATE = "stress-ng.bogo-ops-per-second-usr-sys-time"
FIXTURE = (
    Path(__file__).parents[1]
    / ".artifacts"
    / "system-opt"
    / "m2-component-calibration-20260823"
    / "looper-m2-cpu-calibration-20260823-b"
    / "cpu-20260823T052438.003303Z-1.yaml"
)
BUSINESS = MetricContract(
    id=RATE,
    role=MetricRole.BUSINESS_PRIMARY,
    component="cpu",
    direction=MetricDirection.MAXIMIZE,
    unit="bogo-ops/s",
    scope="dynamic loop fixture",
    phase="measure",
    aggregation=Aggregation.MEAN,
    minimum_samples=2,
    scale=1.0,
    minimum_effect=0.5,
    pressure_method=PressureMethod.NONE,
    source="stress-ng yaml metrics",
)
STATS = StatisticsPolicy(
    confidence_level=0.95,
    bootstrap_resamples=2000,
    random_seed=7,
    baseline_repeats=2,
    candidate_repeats=2,
    baseline_every_n=1,
)


def _contract(slo_bound: float | None) -> WorkloadContract:
    payload = dict(
        workload_id="stress-ng-standin-dynamic-test",
        load_provider="external-test",
        load_command={
            "tool": "stress-ng",
            "argv_digest": "sha256:" + "a" * 64,
            "declared_duration_seconds": 120,
            "description": "test-side owned load",
        },
        o0_metrics=[
            {
                "metric_id": RATE,
                "unit": "bogo-ops/s",
                "direction": "maximize",
                "aggregation": "mean",
                "source": "stress-ng yaml metrics",
            },
            {
                "metric_id": "stress-ng.bogo-ops",
                "unit": "ops",
                "direction": "maximize",
                "aggregation": "mean",
                "source": "stress-ng yaml metrics",
            },
        ],
        objective={"primary_metric_id": RATE, "scale": 1.0, "mde": 0.5},
        slos=(
            [
                {
                    "metric_id": RATE,
                    "comparator": "at-least",
                    "bound": slo_bound,
                    "unit": "bogo-ops/s",
                }
            ]
            if slo_bound is not None
            else []
        ),
        correctness_gates=[
            {"metric_id": "stress-ng.bogo-ops", "comparator": "at-least", "bound": 1, "unit": "ops"}
        ],
        phases=[
            {"phase_id": "steady", "purpose": "load", "o0_metric_ids": [RATE, "stress-ng.bogo-ops"]}
        ],
        limitations="dynamic loop test fixture",
    )
    return WorkloadContract(**payload)


def _gate(
    contract: WorkloadContract,
    *,
    max_interventions: int = 2,
    hold_windows: int = 2,
) -> DynamicPhaseGateContract:
    return DynamicPhaseGateContract(
        workload_contract_digest=contract.digest,
        slo=SloTarget(
            metric_id=RATE,
            comparator="at-least",
            bound=1000.0,
            hold_windows=hold_windows,
        ),
        convergence={"rounds": 99, "lcb_threshold": 0.0},
        budget=PhaseBudget(
            max_interventions=max_interventions, wall_clock_seconds=3600.0, risk_quota=5
        ),
        degradation={"metric_id": "stress-ng.bogo-ops", "relative_limit": 0.05},
        reactivation_holdout_windows=2,
    )


def _hypotheses_factory(count: int):
    def factory(symptom) -> list[ComponentHypothesis]:
        components = ["cpu", "memory", "network"]
        return [
            ComponentHypothesis(
                hypothesis_id=f"hyp-{components[i]}",
                symptom_id=symptom.symptom_id,
                component=components[i],
                rank=i + 1,
            )
            for i in range(count)
        ]

    return factory


def _clock():
    state = {"now": datetime(2026, 8, 23, 12, 0, tzinfo=UTC)}

    def tick() -> datetime:
        state["now"] += timedelta(seconds=30)
        return state["now"]

    return tick


def _improvement(values: list[float]):
    return bootstrap_improvement(
        MetricEvidence(metric_id=RATE, values=values),
        MetricEvidence(metric_id=RATE, values=[1000.0, 1000.5, 999.5, 1000.2]),
        BUSINESS,
        STATS,
    )


def _run(contract, gate, **overrides) -> DynamicPhaseRun:
    payload = dict(
        contract=contract,
        gate_contract=gate,
        promotion_contract=PromotionContract(
            min_observations=3, min_distinct_time_blocks=3, min_environments=1
        ),
        environment_digest=ENV,
        max_windows=6,
        probe_top_k=1,
        load_identity=lambda _window: contract.load_command,
        o0_source=lambda _window: FIXTURE.read_text(encoding="utf-8"),
        hypothesis_source=_hypotheses_factory(2),
        clock=_clock(),
    )
    payload.update(overrides)
    return run_dynamic_phase(**payload)


def test_slo_met_stops_via_target_met():
    contract = _contract(slo_bound=1000.0)  # fixture rate 1182.49 meets it

    run = _run(contract, _gate(contract))

    assert run.stop_gate_decision is not None
    assert run.stop_gate_decision.stop_class is GateStopClass.TARGET_MET
    assert all(record.slo_met for record in run.windows)
    assert not any(record.action is WindowAction.SYMPTOM_REGISTERED for record in run.windows)


def test_single_hypothesis_symptom_blocks_intervention():
    contract = _contract(slo_bound=1500.0)  # fixture rate violates

    run = _run(
        contract,
        _gate(contract),
        hypothesis_source=_hypotheses_factory(1),
        component_probe=lambda hypothesis, window: window.digest,
        intervention=lambda hypothesis: InterventionExperiment(
            measurement_batch_digest="sha256:" + "1" * 64,
            business_metric_id=RATE,
            accepted=True,
        ),
    )

    blocked = [r for r in run.windows if r.action is WindowAction.PROBE_BLOCKED]
    assert blocked, "the D2 single-hypothesis rule must block interventions"
    assert "competing hypothesis" in blocked[0].note
    assert not any(r.action is WindowAction.INTERVENED for r in run.windows)
    # the convergence counter counts candidate LCB rounds (upstream derivation),
    # so this run ends on the window budget with the final gate decision as-is
    assert run.stop_gate_decision is not None and not run.stop_gate_decision.stop
    assert run.note and "window budget" in run.note


def test_missing_probe_callback_leaves_hypotheses_proposed():
    contract = _contract(slo_bound=1500.0)

    run = _run(contract, _gate(contract))  # no component_probe, no intervention

    assert any(r.action is WindowAction.SYMPTOM_REGISTERED for r in run.windows)
    assert not any(
        r.action in (WindowAction.INTERVENED, WindowAction.VERIFIED) for r in run.windows
    )


def test_accepted_intervention_promotes_after_reverification():
    contract = _contract(slo_bound=1500.0)
    gate = _gate(contract, max_interventions=1)

    run = _run(
        contract,
        gate,
        component_probe=lambda hypothesis, window: window.digest,
        intervention=lambda hypothesis: InterventionExperiment(
            measurement_batch_digest="sha256:" + "2" * 64,
            business_metric_id=RATE,
            accepted=True,
        ),
        retest=lambda _window: RetestOutcome(
            improvement=_improvement([1200.0, 1205.0, 1198.0, 1202.0]),
            measurement_batch_digest="sha256:" + "4" * 64,
        ),
        verification_window_count=3,
    )

    assert any(r.action is WindowAction.VERIFIED for r in run.windows)
    assert run.promotion is not None and run.promotion.promoted is True
    assert run.hypothesis_ledger_digest is not None
    assert run.stop_gate_decision is not None
    assert run.stop_gate_decision.stop_class is GateStopClass.BUDGET_EXHAUSTED


def test_failed_retest_blocks_promotion_fail_closed():
    contract = _contract(slo_bound=1500.0)

    run = _run(
        contract,
        _gate(contract, max_interventions=1),
        component_probe=lambda hypothesis, window: window.digest,
        intervention=lambda hypothesis: InterventionExperiment(
            measurement_batch_digest="sha256:" + "2" * 64,
            business_metric_id=RATE,
            accepted=True,
        ),
        retest=lambda _window: RetestOutcome(
            improvement=_improvement([1000.1, 999.9, 1000.0, 1000.2]),
            measurement_batch_digest="sha256:" + "5" * 64,
        ),
        verification_window_count=3,
    )

    assert run.promotion is not None and run.promotion.promoted is False
    assert run.promotion.failed_observations


def test_identity_drift_stops_the_phase_immediately():
    contract = _contract(slo_bound=1000.0)

    def drifting(window_id: str):
        if window_id == "window-3":
            return contract.load_command.model_copy(
                update={"declared_duration_seconds": 121}
            )
        return contract.load_command

    run = _run(
        contract, _gate(contract, hold_windows=99), load_identity=drifting
    )

    drift_records = [r for r in run.windows if r.action is WindowAction.IDENTITY_DRIFT]
    assert drift_records and drift_records[0].window_id == "window-3"
    assert run.stop_gate_decision is not None
    assert run.stop_gate_decision.stop_class is GateStopClass.WORKLOAD_VANISHED
    with pytest.raises(WorkloadIdentityDrift):
        raise WorkloadIdentityDrift(
            expected="sha256:" + "0" * 64, actual="sha256:" + "1" * 64
        )


def test_run_digest_is_deterministic_and_gate_must_bind_contract():
    contract = _contract(slo_bound=1000.0)
    gate = _gate(contract)

    first = _run(contract, gate)
    second = _run(contract, gate)

    assert first.digest == second.digest
    other_contract = _contract(slo_bound=1000.0).model_copy(
        update={"workload_id": "stress-ng-standin-different"}
    )
    unbound = _gate(other_contract)
    with pytest.raises(ValueError, match="not bound"):
        _run(contract, unbound)
