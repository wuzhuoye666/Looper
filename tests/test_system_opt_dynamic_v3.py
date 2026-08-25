"""DYN-END-01I: v3 gate / evaluator / loop tests for the explicit window endpoint.

方案 A（gate schema v1alpha3）: ``max_windows`` lives in ``PhaseBudgetV3``, is
bound by the contract digest, and is enforced inside ``evaluate_phase_gate_v3``
so every normal v3 return carries ``stop=true`` with the last window's evidence
digest. v1/v2 models and digests are untouched.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from looper_core.system_opt.dynamic_loop import run_dynamic_phase_v3
from looper_core.system_opt.observation import parse_o0_metrics
from looper_core.system_opt.phase_gate import (
    DYNAMIC_PHASE_GATE_SCHEMA,
    DYNAMIC_PHASE_GATE_V3_SCHEMA,
    ConvergencePolicy,
    DegradationGate,
    DynamicPhaseGateContract,
    DynamicPhaseGateContractV3,
    GateStopClass,
    PhaseBudget,
    PhaseBudgetV3,
    PhaseGateState,
    SloTarget,
    evaluate_phase_gate_v3,
    load_dynamic_phase_gate,
)
from looper_core.system_opt.result_vector import PromotionContract
from looper_core.system_opt.workload import (
    CorrectnessGate,
    LoadCommandIdentity,
    O0MetricDirection,
    O0MetricSpec,
    WorkloadContract,
    WorkloadObjective,
    WorkloadPhaseSpec,
    load_argv_digest,
)
from pydantic import ValidationError

EVIDENCE = "sha256:" + "e" * 64
CONTRACT = "sha256:" + "c" * 64
BUSINESS = "stress-ng.bogo-ops-per-second-usr-sys-time"


def _v3_gate(
    *, max_windows: int, max_interventions: int = 5, workload_contract_digest: str = CONTRACT
) -> DynamicPhaseGateContractV3:
    return DynamicPhaseGateContractV3(
        workload_contract_digest=workload_contract_digest,
        slo=SloTarget(
            metric_id=BUSINESS, comparator="at-least", bound=1000.0, hold_windows=2
        ),
        convergence=ConvergencePolicy(rounds=4, lcb_threshold=0.0),
        budget=PhaseBudgetV3(
            max_interventions=max_interventions,
            wall_clock_seconds=3600.0,
            risk_quota=1,
            max_windows=max_windows,
        ),
        degradation=DegradationGate(metric_id="stress-ng.bogo-ops", relative_limit=0.05),
        reactivation_holdout_windows=2,
    )


def _state(**overrides) -> PhaseGateState:
    payload: dict = dict(
        consecutive_slo_met_windows=0,
        consecutive_lcb_threshold_rounds=0,
        interventions=0,
        risky_interventions=0,
        elapsed_seconds=0.0,
        windows_observed=0,
        identity_drift_events=0,
        degradation_events=0,
        evidence_digest=EVIDENCE,
    )
    payload.update(overrides)
    return PhaseGateState(**payload)


class TestV3GateSchema:
    def test_max_windows_is_required(self):
        with pytest.raises(ValidationError):
            DynamicPhaseGateContractV3(
                workload_contract_digest=CONTRACT,
                slo=None,
                convergence=ConvergencePolicy(rounds=4, lcb_threshold=0.0),
                budget=PhaseBudgetV3(
                    max_interventions=5, wall_clock_seconds=3600.0, risk_quota=1
                ),
                degradation=DegradationGate(
                    metric_id="stress-ng.bogo-ops", relative_limit=0.05
                ),
                reactivation_holdout_windows=2,
            )

    def test_max_windows_must_be_at_least_one(self):
        with pytest.raises(ValidationError):
            PhaseBudgetV3(
                max_interventions=5, wall_clock_seconds=3600.0, risk_quota=1, max_windows=0
            )

    def test_phase_budget_is_backward_compatible(self):
        legacy = PhaseBudget(max_interventions=5, wall_clock_seconds=3600.0, risk_quota=1)
        payload = legacy.model_dump(mode="json")
        assert "max_windows" not in payload


class TestV3Evaluator:
    def test_window_budget_fires_at_or_above_limit(self):
        decision = evaluate_phase_gate_v3(
            _v3_gate(max_windows=3), _state(windows_observed=3)
        )
        assert decision.stop and decision.stop_class is GateStopClass.BUDGET_EXHAUSTED
        assert decision.triggered_field == "budget.max_windows"

    def test_window_budget_does_not_fire_below_limit(self):
        decision = evaluate_phase_gate_v3(
            _v3_gate(max_windows=3), _state(windows_observed=2)
        )
        assert not decision.stop

    def test_safety_and_identity_beat_window_budget(self):
        for overrides, field in (
            ({"degradation_events": 1, "windows_observed": 9}, "degradation"),
            ({"identity_drift_events": 1, "windows_observed": 9}, "identity_drift_action"),
        ):
            decision = evaluate_phase_gate_v3(_v3_gate(max_windows=3), _state(**overrides))
            assert decision.stop and decision.triggered_field == field

    def test_interventions_and_wall_clock_beat_window_budget(self):
        for overrides, field in (
            ({"interventions": 5, "windows_observed": 9}, "budget.max_interventions"),
            ({"elapsed_seconds": 3600.0, "windows_observed": 9}, "budget.wall_clock_seconds"),
        ):
            decision = evaluate_phase_gate_v3(_v3_gate(max_windows=3), _state(**overrides))
            assert decision.stop and decision.triggered_field == field

    def test_window_budget_beats_slo(self):
        decision = evaluate_phase_gate_v3(
            _v3_gate(max_windows=3),
            _state(windows_observed=3, consecutive_slo_met_windows=9),
        )
        assert decision.stop
        assert decision.triggered_field == "budget.max_windows"
        assert decision.stop_class is GateStopClass.BUDGET_EXHAUSTED

    def test_decision_digest_binds_contract_and_evidence(self):
        gate = _v3_gate(max_windows=3)
        decision = evaluate_phase_gate_v3(gate, _state(windows_observed=3))
        assert decision.contract_digest == gate.digest
        assert decision.evidence_digest == EVIDENCE


class TestVersionDispatch:
    def test_v3_dispatch_and_digest_stability(self):
        gate = _v3_gate(max_windows=3)
        loaded = load_dynamic_phase_gate(gate.model_dump(mode="json"))
        assert isinstance(loaded, DynamicPhaseGateContractV3)
        assert loaded.digest == gate.digest
        assert loaded.schema_version == DYNAMIC_PHASE_GATE_V3_SCHEMA

    def test_v1_and_v2_dispatch_is_unchanged(self):
        v1 = DynamicPhaseGateContract(
            workload_contract_digest=CONTRACT,
            slo=SloTarget(metric_id=BUSINESS, comparator="at-least", bound=1.0, hold_windows=1),
            convergence=ConvergencePolicy(rounds=4, lcb_threshold=0.0),
            budget=PhaseBudget(max_interventions=5, wall_clock_seconds=3600.0, risk_quota=1),
            degradation=DegradationGate(metric_id="stress-ng.bogo-ops", relative_limit=0.05),
            reactivation_holdout_windows=2,
        )
        loaded = load_dynamic_phase_gate(v1.model_dump(mode="json"))
        assert loaded.schema_version == DYNAMIC_PHASE_GATE_SCHEMA
        assert loaded.digest == v1.digest

    def test_unknown_schema_is_rejected(self):
        with pytest.raises(ValueError, match="unsupported dynamic phase gate"):
            load_dynamic_phase_gate({"schema_version": "looper.dynamic-phase-gate/v9"})


def _no_slo_contract() -> WorkloadContract:
    argv = ["stress-ng", "--cpu", "1", "--timeout", "10"]
    identity = LoadCommandIdentity(
        tool="stress-ng",
        argv_digest=load_argv_digest(argv),
        declared_duration_seconds=10.0,
        description="v3 window-budget loop fixture",
    )
    return WorkloadContract(
        workload_id="v3-window-budget-fixture",
        load_command=identity,
        o0_metrics=[
            O0MetricSpec(
                metric_id=BUSINESS,
                unit="bogo-ops/s",
                direction=O0MetricDirection.MAXIMIZE,
                aggregation="mean",
                source="stress-ng yaml",
            ),
            O0MetricSpec(
                metric_id="stress-ng.bogo-ops",
                unit="ops",
                direction=O0MetricDirection.MAXIMIZE,
                aggregation="mean",
                source="stress-ng yaml",
            ),
        ],
        objective=WorkloadObjective(primary_metric_id=BUSINESS, scale=1.0, mde=0.0),
        correctness_gates=[
            CorrectnessGate(
                metric_id="stress-ng.bogo-ops",
                comparator="at-least",
                bound=1.0,
                unit="ops",
            )
        ],
        phases=[
            WorkloadPhaseSpec(
                phase_id="steady", purpose="load", o0_metric_ids=[BUSINESS, "stress-ng.bogo-ops"]
            )
        ],
        limitations="v3 loop fixture",
    )


def _o0() -> str:
    return (
        "metrics:\n- stressor: cpu\n  bogo-ops: 12000\n"
        "  bogo-ops-per-second-usr-sys-time: 100.0\n"
    )


def _clock():
    state = {"now": datetime(2026, 8, 24, 8, 0, tzinfo=UTC)}

    def tick() -> datetime:
        state["now"] += timedelta(seconds=1)
        return state["now"]

    return tick


def _run_v3(contract: WorkloadContract, max_windows: int):
    gate = _v3_gate(max_windows=max_windows, workload_contract_digest=contract.digest)
    return gate, run_dynamic_phase_v3(
        contract=contract,
        gate_contract=gate,
        manifest=None,  # type: ignore[arg-type]  # not used without a symptom
        promotion_contract=PromotionContract(
            min_observations=2, min_distinct_time_blocks=2, min_environments=1
        ),
        environment_digest="sha256:" + "b" * 64,
        probe_top_k=1,
        load_identity=lambda _window: contract.load_command,
        o0_source=lambda _window: _o0(),
        hypothesis_source=lambda _symptom: [],
        prepare_intervention=lambda _h: None,  # type: ignore[arg-type]  # never called
        execute_intervention=lambda _p, _w: None,  # type: ignore[arg-type]  # never called
        clock=_clock(),
    )


class TestV3Loop:
    def test_window_budget_stops_with_last_window_evidence(self):
        contract = _no_slo_contract()
        gate, run = _run_v3(contract, max_windows=3)
        assert run.stop_gate_decision is not None
        assert run.stop_gate_decision.stop is True
        assert run.stop_gate_decision.stop_class is GateStopClass.BUDGET_EXHAUSTED
        assert run.stop_gate_decision.triggered_field == "budget.max_windows"
        assert len(run.windows) == 3
        assert (
            run.stop_gate_decision.evidence_digest
            == run.windows[-1].observation_window_digest
        )
        assert run.stop_gate_decision.contract_digest == gate.digest

    def test_single_window_budget_stops_immediately(self):
        contract = _no_slo_contract()
        _, run = _run_v3(contract, max_windows=1)
        assert run.stop_gate_decision is not None
        assert run.stop_gate_decision.stop is True
        assert run.stop_gate_decision.triggered_field == "budget.max_windows"
        assert len(run.windows) == 1

    def test_o0_is_parsed_and_windows_observed_counted(self):
        contract = _no_slo_contract()
        parsed = parse_o0_metrics("stress-ng", [BUSINESS], _o0())
        assert parsed[BUSINESS] == [100.0]
        _, run = _run_v3(contract, max_windows=3)
        assert all(window.observation_window_digest for window in run.windows)
        assert all(window.action.value == "observe" for window in run.windows)
