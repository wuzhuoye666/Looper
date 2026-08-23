"""C1 characterization: pin today's dual-adjudication behavior (tuning vs judge).

Purpose: safety net for the registered L5 refactor ("终裁上收",
layer-specifications.md §1 item 5). These tests pin the CURRENT semantics —
including the known divergences flagged by the 2026-08-23 external review —
so the refactor either preserves them deliberately or trips these tests
loudly. They are not endorsements of the current behavior.
"""

from __future__ import annotations

import math

from looper_core.system_opt.demo import (
    SyntheticMeasurementAdapter,
    build_demo_manifest,
    build_demo_policy,
    resolve_demo_domains,
)
from looper_core.system_opt.engine.judge import evaluate_candidate
from looper_core.system_opt.executor.simulated import SimulatedBackend
from looper_core.system_opt.policy import (
    Aggregation,
    MetricContract,
    MetricDirection,
    MetricRole,
    OptimizationMode,
    PressureMethod,
    StatisticsPolicy,
)
from looper_core.system_opt.safety import SafetyState
from looper_core.system_opt.scoring import MetricEvidence, bootstrap_improvement
from looper_core.system_opt.tuning import CandidateEvaluation, SystemOptimizationEngine

DIGEST = "sha256:" + "c" * 64


def _passed_gate(metric: str = "cpu.success") -> object:
    from looper_core.system_opt.scoring import GateEvidence

    return GateEvidence(
        gate_id=f"{metric}.gate",
        metric=metric,
        actual=True,
        passed=True,
        reason="characterization fixture gate",
    )


def _evaluation(**overrides) -> CandidateEvaluation:
    defaults: dict = dict(
        round_index=1,
        attempt_index=1,
        candidate_id="cand-1",
        parameters={"vm.swappiness": 10},
        change_count=1,
        safety_state=SafetyState.ROLLED_BACK,
        comparison_baseline_digest=DIGEST,
        comparable=True,
        identity_mismatches=[],
        gates=[_passed_gate()],
        improvements={},
        feasible=True,
        accepted=False,
    )
    defaults.update(overrides)
    return CandidateEvaluation(**defaults)


def _contract(minimum_effect: float = 0.5) -> MetricContract:
    return MetricContract(
        id="cpu.score",
        role=MetricRole.BUSINESS_PRIMARY,
        component="cpu",
        direction=MetricDirection.MAXIMIZE,
        unit="ops/s",
        scope="characterization fixture",
        phase="measure",
        aggregation=Aggregation.MEAN,
        minimum_samples=2,
        scale=1.0,
        minimum_effect=minimum_effect,
        pressure_method=PressureMethod.NONE,
        source="characterization fixture",
    )


class TestEngineAgreesWithComponentSuggestion:
    """Refactor invariant: for identical evidence the L8 judge must never
    disagree with the L5 component's promotion suggestion on comparable /
    feasible / accepted, and the rejection reason must follow S0->S2->S7."""

    def test_full_simulated_run_verdicts_match_component_suggestions(self):
        manifest = build_demo_manifest()
        initial = {item.id: item.default for item in manifest.items}
        backend = SimulatedBackend(initial, target_id="characterization-run")
        policy = build_demo_policy(OptimizationMode.GENERAL)
        policy.authorized_components = ["cpu"]
        engine = SystemOptimizationEngine(
            policy, manifest, resolve_demo_domains(manifest), backend
        )
        run = engine.run(
            baseline_parameters={item.parameter_id: item.default for item in manifest.items},
            measure=SyntheticMeasurementAdapter(backend, mode=OptimizationMode.GENERAL),
            fencing_token=1,
        )
        assert run.candidates, "the demo run must evaluate at least one candidate"
        primary = policy.primary_metric
        for candidate in run.candidates:
            verdict = evaluate_candidate(
                candidate,
                primary_metric=primary.id,
                minimum_effect=primary.minimum_effect or 0.0,
            )
            assert verdict.comparable == candidate.comparable
            assert verdict.feasible == candidate.feasible
            assert verdict.accepted == candidate.accepted
            if not verdict.comparable:
                assert verdict.reasons[0].startswith("S0:")
            elif not verdict.feasible:
                assert verdict.reasons[0].startswith("S2:")
            else:
                assert verdict.reasons[0].startswith("S7:")


class TestJudgeDerivesFeasibilityFromFieldsNotSafetyState:
    """Known divergence (review C1): the judge never reads safety_state or
    safety_reason; feasibility comes from the candidate fields the L5 engine
    already computed (which DO include the SafetyState.ROLLED_BACK
    requirement). When adjudication moves fully to L8 this must become an
    explicit judge-owned safety semantic — this test pins today's behavior.
    """

    def test_safety_attention_is_invisible_to_the_judge_reason(self):
        candidate = _evaluation(
            safety_state=SafetyState.NEEDS_ATTENTION,
            safety_reason="candidate rollback needs attention",
            feasible=False,
        )
        verdict = evaluate_candidate(
            candidate, primary_metric="cpu.score", minimum_effect=0.0
        )

        assert verdict.accepted is False
        assert verdict.reasons == [
            "S2: hard gates failed ['feasible=false']; "
            "no improvement can compensate a gate failure"
        ]
        assert all("attention" not in reason for reason in verdict.reasons)

    def test_identity_mismatch_is_rejected_at_s0_before_gates(self):
        candidate = _evaluation(
            comparable=False,
            identity_mismatches=["environment"],
            gates=[],
            feasible=False,
        )
        verdict = evaluate_candidate(
            candidate, primary_metric="cpu.score", minimum_effect=0.0
        )

        assert verdict.comparable is False
        assert verdict.reasons[0].startswith("S0: identity mismatch ['environment']")

    def test_missing_primary_improvement_is_rejected_at_s7(self):
        candidate = _evaluation(improvements={})
        verdict = evaluate_candidate(
            candidate, primary_metric="cpu.score", minimum_effect=0.0
        )

        assert verdict.comparable and verdict.feasible and not verdict.accepted
        assert verdict.reasons[0].startswith("S7: primary metric 'cpu.score' has no improvement")


class TestBootstrapGoldenNumbers:
    """Pin the exact S6/S7 numerics for a fixed fixture (seed 7, 2000
    resamples, two-sided percentile bounds). Any change to quantile
    interpolation, resampling order, or seed handling must update these
    golden values deliberately (and bump the formula version).
    """

    BASELINE = [100.0, 100.5, 99.5, 100.2]
    CANDIDATE = [102.0, 102.4, 101.8, 102.2]
    GOLDEN_ESTIMATE = 2.049999999999997
    GOLDEN_LOWER = 1.6500000000000057
    GOLDEN_UPPER = 2.4750000000000085

    def _statistics(self) -> StatisticsPolicy:
        return StatisticsPolicy(
            confidence_level=0.95,
            bootstrap_resamples=2000,
            random_seed=7,
            baseline_repeats=2,
            candidate_repeats=2,
            baseline_every_n=1,
        )

    def test_golden_bounds_are_exact_and_deterministic(self):
        contract = _contract()
        baseline = MetricEvidence(metric_id="cpu.score", values=self.BASELINE)
        candidate = MetricEvidence(metric_id="cpu.score", values=self.CANDIDATE)

        evidence = bootstrap_improvement(candidate, baseline, contract, self._statistics())
        repeat = bootstrap_improvement(candidate, baseline, contract, self._statistics())

        assert evidence.estimate == self.GOLDEN_ESTIMATE
        assert evidence.lower == self.GOLDEN_LOWER
        assert evidence.upper == self.GOLDEN_UPPER
        assert evidence.formula_id == "F-PROJECT-S6-S7/v1alpha1"
        assert (evidence.lower, evidence.upper) == (repeat.lower, repeat.upper)

    def test_acceptance_flips_exactly_at_the_lcb_boundary(self):
        contract = _contract()
        baseline = MetricEvidence(metric_id="cpu.score", values=self.BASELINE)
        candidate = MetricEvidence(metric_id="cpu.score", values=self.CANDIDATE)
        evidence = bootstrap_improvement(candidate, baseline, contract, self._statistics())
        accepted = _evaluation(
            improvements={"cpu.score": evidence},
        )

        at_bound = evaluate_candidate(
            accepted, primary_metric="cpu.score", minimum_effect=evidence.lower
        )
        below_bound = evaluate_candidate(
            accepted,
            primary_metric="cpu.score",
            minimum_effect=math.nextafter(evidence.lower, -math.inf),
        )

        assert at_bound.accepted is False, "S7 requires strictly lower > MDE"
        assert below_bound.accepted is True
