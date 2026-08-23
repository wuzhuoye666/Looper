"""M3 dynamic-phase ending gate contract tests (workload-tuning.md D4)."""

from __future__ import annotations

import pytest
from looper_core.system_opt.phase_gate import (
    DynamicPhaseGateContract,
    GateDecision,
    GateStopClass,
    PhaseBudget,
    PhaseGateState,
    SloTarget,
    evaluate_phase_gate,
)
from pydantic import ValidationError

CONTRACT_DIGEST = "sha256:" + "d" * 64
EVIDENCE = "sha256:" + "e" * 64


def _contract(**overrides) -> DynamicPhaseGateContract:
    payload: dict = dict(
        workload_contract_digest=CONTRACT_DIGEST,
        slo=SloTarget(
            metric_id="stress-ng.bogo-ops-per-second-usr-sys-time",
            comparator="at-least",
            bound=1000.0,
            hold_windows=3,
        ),
        convergence={"rounds": 4, "lcb_threshold": 0.0},
        budget=PhaseBudget(
            max_interventions=5, wall_clock_seconds=3600.0, risk_quota=1
        ),
        degradation={"metric_id": "stress-ng.bogo-ops", "relative_limit": 0.05},
        reactivation_holdout_windows=2,
    )
    payload.update(overrides)
    return DynamicPhaseGateContract(**payload)


def _state(**overrides) -> PhaseGateState:
    payload: dict = dict(
        consecutive_slo_met_windows=0,
        consecutive_lcb_threshold_rounds=0,
        interventions=0,
        risky_interventions=0,
        elapsed_seconds=0.0,
        identity_drift_events=0,
        degradation_events=0,
        evidence_digest=EVIDENCE,
    )
    payload.update(overrides)
    return PhaseGateState(**payload)


class TestContract:
    def test_digest_is_deterministic_and_binds_the_workload_contract(self):
        contract = _contract()
        rebuilt = DynamicPhaseGateContract.model_validate(contract.model_dump(mode="python"))

        assert contract.digest == rebuilt.digest
        assert contract.workload_contract_digest == CONTRACT_DIGEST
        assert contract.single_change_per_window is True
        assert contract.identity_drift_action == "stop-phase"

    def test_slo_is_optional_but_other_stop_classes_are_not(self):
        assert _contract(slo=None).slo is None
        with pytest.raises(ValidationError):
            _contract(budget=None)
        with pytest.raises(ValidationError):
            _contract(convergence=None)
        with pytest.raises(ValidationError):
            _contract(degradation=None)


class TestEvaluationOrder:
    def test_safety_beats_everything_even_with_all_classes_firing(self):
        decision = evaluate_phase_gate(
            _contract(),
            _state(
                degradation_events=1,
                identity_drift_events=1,
                interventions=99,
                consecutive_slo_met_windows=99,
                consecutive_lcb_threshold_rounds=99,
            ),
        )

        assert decision.stop
        assert decision.stop_class is GateStopClass.SAFETY_TRIGGERED
        assert decision.triggered_field == "degradation"

    def test_identity_drift_beats_budget_and_targets(self):
        decision = evaluate_phase_gate(
            _contract(),
            _state(identity_drift_events=1, interventions=99),
        )

        assert decision.stop_class is GateStopClass.WORKLOAD_VANISHED

    def test_budget_fires_in_declared_dimension(self):
        for overrides, field in (
            ({"interventions": 5}, "budget.max_interventions"),
            ({"elapsed_seconds": 3600.0}, "budget.wall_clock_seconds"),
            ({"risky_interventions": 2}, "budget.risk_quota"),
        ):
            decision = evaluate_phase_gate(_contract(), _state(**overrides))
            assert decision.stop_class is GateStopClass.BUDGET_EXHAUSTED
            assert decision.triggered_field == field

    def test_target_met_requires_the_full_hold_window_count(self):
        contract = _contract()

        below = evaluate_phase_gate(_contract(), _state(consecutive_slo_met_windows=2))
        met = evaluate_phase_gate(
            contract, _state(consecutive_slo_met_windows=3)
        )

        assert not below.stop
        assert met.stop and met.stop_class is GateStopClass.TARGET_MET
        assert met.triggered_field == "slo.hold_windows"

    def test_convergence_fires_last_and_only_at_threshold_rounds(self):
        contract = _contract()

        early = evaluate_phase_gate(
            contract, _state(consecutive_lcb_threshold_rounds=3)
        )
        hit = evaluate_phase_gate(
            contract, _state(consecutive_lcb_threshold_rounds=4)
        )

        assert not early.stop
        assert hit.stop and hit.stop_class is GateStopClass.CONVERGED

    def test_continuing_decision_carries_no_stop_class(self):
        decision = evaluate_phase_gate(_contract(), _state())

        assert not decision.stop
        assert decision.stop_class is None and decision.triggered_field is None
        assert decision.evidence_digest == EVIDENCE


class TestDecisionModel:
    def test_stopping_decision_must_cite_class_and_field(self):
        with pytest.raises(ValidationError, match="cite its stop class"):
            GateDecision(
                stop=True,
                reason="incomplete",
                contract_digest=CONTRACT_DIGEST,
                evidence_digest=EVIDENCE,
            )

    def test_continuing_decision_must_not_cite_class(self):
        with pytest.raises(ValidationError, match="must not cite"):
            GateDecision(
                stop=False,
                stop_class=GateStopClass.CONVERGED,
                triggered_field="convergence.rounds",
                reason="inconsistent",
                contract_digest=CONTRACT_DIGEST,
                evidence_digest=EVIDENCE,
            )
