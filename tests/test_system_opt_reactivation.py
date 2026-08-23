"""M3 reactivation eligibility tests (workload-tuning.md D5, SO-D019 A+B)."""

from __future__ import annotations

import pytest
from looper_core.system_opt.phase_gate import (
    DynamicPhaseGateContract,
    PhaseBudget,
    SloTarget,
)
from looper_core.system_opt.reactivation import (
    ReactivationDecision,
    ReactivationPolicy,
    ReactivationState,
    ReactivationTrigger,
    evaluate_reactivation,
)
from pydantic import ValidationError

GATE_DIGEST = "sha256:" + "f" * 64
EVIDENCE = "sha256:" + "3" * 64


def _gate(holdout: int = 2) -> DynamicPhaseGateContract:
    return DynamicPhaseGateContract(
        workload_contract_digest=GATE_DIGEST,
        slo=SloTarget(
            metric_id="stress-ng.bogo-ops-per-second-usr-sys-time",
            comparator="at-least",
            bound=1000.0,
            hold_windows=3,
        ),
        convergence={"rounds": 4, "lcb_threshold": 0.0},
        budget=PhaseBudget(max_interventions=5, wall_clock_seconds=3600.0, risk_quota=1),
        degradation={"metric_id": "stress-ng.bogo-ops", "relative_limit": 0.05},
        reactivation_holdout_windows=holdout,
    )


def _policy(**overrides) -> ReactivationPolicy:
    payload = dict(max_reactivations=2, slo_violation_windows=3)
    payload.update(overrides)
    return ReactivationPolicy(**payload)


def _state(**overrides) -> ReactivationState:
    payload = dict(
        reactivations_used=0,
        windows_since_stop=2,
        consecutive_slo_violations=0,
        identity_drift_events_since_stop=0,
    )
    payload.update(overrides)
    return ReactivationState(**payload)


def _evaluate(state: ReactivationState, policy=None, gate=None):
    return evaluate_reactivation(
        gate or _gate(), policy or _policy(), state, evidence_digest=EVIDENCE
    )


class TestEligibility:
    def test_identity_drift_grants_immediate_eligibility_after_holdout(self):
        decision = _evaluate(_state(identity_drift_events_since_stop=1))

        assert decision.eligible
        assert decision.trigger is ReactivationTrigger.IDENTITY_DRIFT

    def test_slo_persistence_grants_eligibility_only_at_threshold(self):
        below = _evaluate(_state(consecutive_slo_violations=2))
        at = _evaluate(_state(consecutive_slo_violations=3))

        assert not below.eligible
        assert at.eligible and at.trigger is ReactivationTrigger.SLO_VIOLATION_PERSISTENCE

    def test_no_trigger_means_not_eligible(self):
        decision = _evaluate(_state())

        assert not decision.eligible and decision.trigger is None


class TestGuards:
    def test_exhausted_budget_blocks_even_identity_drift(self):
        decision = _evaluate(
            _state(reactivations_used=2, identity_drift_events_since_stop=1)
        )

        assert not decision.eligible
        assert "budget exhausted" in decision.reason

    def test_holdout_blocks_even_identity_drift(self):
        decision = _evaluate(
            _state(identity_drift_events_since_stop=1, windows_since_stop=1)
        )

        assert not decision.eligible
        assert "holdout" in decision.reason

    def test_drift_beats_slo_persistence_when_both_fire(self):
        decision = _evaluate(
            _state(identity_drift_events_since_stop=1, consecutive_slo_violations=9)
        )

        assert decision.trigger is ReactivationTrigger.IDENTITY_DRIFT


class TestDecisionModel:
    def test_decision_digest_is_deterministic_and_cites_evidence(self):
        one = _evaluate(_state(identity_drift_events_since_stop=1))
        two = _evaluate(_state(identity_drift_events_since_stop=1))

        assert one.digest == two.digest
        assert one.evidence_digest == EVIDENCE
        assert one.gate_contract_digest == _gate().digest

    def test_eligibility_consistency_is_enforced_by_the_model(self):
        with pytest.raises(ValidationError, match="cite its trigger"):
            ReactivationDecision(
                eligible=True,
                reason="inconsistent",
                gate_contract_digest=GATE_DIGEST,
                evidence_digest=EVIDENCE,
            )
        with pytest.raises(ValidationError, match="must not cite"):
            ReactivationDecision(
                eligible=False,
                trigger=ReactivationTrigger.IDENTITY_DRIFT,
                reason="inconsistent",
                gate_contract_digest=GATE_DIGEST,
                evidence_digest=EVIDENCE,
            )

    def test_policy_has_no_hidden_defaults(self):
        with pytest.raises(ValidationError):
            ReactivationPolicy()
        with pytest.raises(ValidationError):
            _policy(slo_violation_windows=0)
