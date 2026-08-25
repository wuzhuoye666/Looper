"""DYN-END-01I: v3 gate / evaluator / loop tests for the explicit window endpoint.

方案 A（gate schema v1alpha3）: ``max_windows`` lives in ``PhaseBudgetV3``, is
bound by the contract digest, and is enforced inside ``evaluate_phase_gate_v3``
so every normal v3 return carries ``stop=true`` with the last window's evidence
digest. v1/v2 models and digests are untouched.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from looper_core.system_opt.dynamic_loop import (
    load_dynamic_phase_run,
    run_dynamic_phase_v3,
)
from looper_core.system_opt.observation import parse_o0_metrics
from looper_core.system_opt.phase_gate import (
    DYNAMIC_PHASE_GATE_SCHEMA,
    DYNAMIC_PHASE_GATE_V2_SCHEMA,
    DYNAMIC_PHASE_GATE_V3_SCHEMA,
    ConvergencePolicy,
    DegradationGate,
    DynamicPhaseGateContract,
    DynamicPhaseGateContractV2,
    DynamicPhaseGateContractV3,
    GateStopClass,
    PhaseBudget,
    PhaseBudgetV3,
    PhaseGateState,
    SloTarget,
    evaluate_phase_gate_v2,
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
FIXTURES = Path(__file__).parent / "fixtures" / "real-demo-2026-08-25"
REAL_GATE_DIGEST = "sha256:c39720e7d9ba817ab1d363c12b8085c6e04afbb511da8605935735787f735dc0"
REAL_WORKLOAD_DIGEST = "sha256:beb2a9e6a25c82597b0d30576f444632defe07810e7a0f9665de144ad9c1ecb1"


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


class TestRealArtifactRegression:
    """N15: load REAL-M3-01 v2 payloads and prove zero digest drift."""

    def _gate_payload(self) -> dict:
        return json.loads((FIXTURES / "gate-contract.json").read_text(encoding="utf-8"))

    def _run_payload(self) -> dict:
        return json.loads((FIXTURES / "dynamic-run.json").read_text(encoding="utf-8"))

    def test_real_v2_gate_recomputes_the_pinned_digest(self):
        gate = load_dynamic_phase_gate(self._gate_payload())
        assert isinstance(gate, DynamicPhaseGateContractV2)
        assert gate.schema_version == DYNAMIC_PHASE_GATE_V2_SCHEMA
        assert gate.digest == REAL_GATE_DIGEST
        assert gate.workload_contract_digest == REAL_WORKLOAD_DIGEST

    def test_real_v2_run_loads_and_binds_the_pinned_gate(self):
        run = load_dynamic_phase_run(self._run_payload())
        assert run.schema_version == "looper.dynamic-phase-run/v1alpha2"
        assert run.gate_contract_digest == REAL_GATE_DIGEST
        assert run.workload_contract_digest == REAL_WORKLOAD_DIGEST
        # The legacy v2 run ended with stop=false and six windows: preserved verbatim.
        assert run.stop_gate_decision is not None
        assert run.stop_gate_decision.stop is False
        assert run.stop_gate_decision.contract_digest == REAL_GATE_DIGEST
        assert len(run.windows) == 6

    def test_real_v2_decision_evidence_is_workload_digest(self):
        run = load_dynamic_phase_run(self._run_payload())
        # The old weak binding: the final decision pointed at the workload contract
        # digest, not the last window. v3 fixes this; the legacy artifact stays intact.
        assert run.stop_gate_decision.evidence_digest == REAL_WORKLOAD_DIGEST
        assert run.stop_gate_decision.evidence_digest != run.windows[-1].observation_window_digest


class TestForgedCrossVersion:
    """N17: a max_windows decision cannot come from a v2 contract."""

    def test_v2_evaluator_never_emits_window_budget_field(self):
        v2 = DynamicPhaseGateContractV2(
            workload_contract_digest=CONTRACT,
            slo=SloTarget(metric_id=BUSINESS, comparator="at-least", bound=1.0, hold_windows=1),
            convergence=ConvergencePolicy(rounds=4, lcb_threshold=0.0),
            budget=PhaseBudget(max_interventions=5, wall_clock_seconds=3600.0, risk_quota=1),
            degradation=DegradationGate(metric_id="stress-ng.bogo-ops", relative_limit=0.05),
            reactivation_holdout_windows=2,
        )
        decision = evaluate_phase_gate_v2(v2, _state(windows_observed=9999))
        assert not decision.stop
        assert decision.triggered_field is None

    def test_v3_evaluator_is_the_only_window_budget_source(self):
        decision = evaluate_phase_gate_v3(_v3_gate(max_windows=3), _state(windows_observed=9999))
        assert decision.stop
        assert decision.triggered_field == "budget.max_windows"


class TestHoldoutSemantics:
    """N18: a window-budget stop is a regular BUDGET_EXHAUSTED stop."""

    def test_window_budget_stop_uses_budget_exhausted_class(self):
        decision = evaluate_phase_gate_v3(_v3_gate(max_windows=3), _state(windows_observed=3))
        assert decision.stop_class is GateStopClass.BUDGET_EXHAUSTED

    def test_reactivation_holdout_field_is_unchanged_in_v3_contract(self):
        gate = _v3_gate(max_windows=3)
        assert gate.reactivation_holdout_windows == 2
        assert "reactivation_holdout_windows" in gate.model_dump(mode="json")
