from __future__ import annotations

from datetime import UTC, datetime

import pytest
from looper_core.system_opt.engine import (
    ComponentScore,
    evaluate_candidate,
    rank_components,
    score_components,
    select_next_candidate,
)
from looper_core.system_opt.negative_cache import (
    NegativeCache,
    NegativeCacheEntry,
    NegativeVerdict,
    candidate_parameters_digest,
    formula_versions_digest,
)
from looper_core.system_opt.safety import SafetyState
from looper_core.system_opt.scoring import DiagnosticPriority, GateEvidence, ImprovementEvidence
from looper_core.system_opt.tuning import CandidateEvaluation

PRIMARY = "cpu.bogo-ops-per-second"
FIXED_AT = datetime(2026, 8, 23, 12, 0, 0, tzinfo=UTC)
ENV = "sha256:" + "1" * 64
PROTOCOL_CPU = "sha256:" + "2" * 64
PROTOCOL_MEMORY = "sha256:" + "3" * 64
FORMULAS = {"F-PROJECT-S6-S7": "v1"}
D = "sha256:" + "4" * 64


def _improvement(lower: float, estimate: float = 0.01) -> ImprovementEvidence:
    return ImprovementEvidence(
        metric_id=PRIMARY,
        formula_id="F-PROJECT-S6-S7/v1",
        baseline_digest=D,
        candidate_digest=D,
        baseline_estimate=100.0,
        candidate_estimate=100.0 * (1 + estimate),
        estimate=estimate,
        lower=lower,
        upper=estimate + 0.02,
        minimum_effect=0.0,
        accepted=lower > 0.0,
    )


def _candidate(**overrides) -> CandidateEvaluation:
    payload = dict(
        round_index=1,
        attempt_index=2,
        candidate_id="cand-1",
        parameters={"system.cpufreq-governor-uniform": "performance"},
        change_count=1,
        safety_state=SafetyState.ROLLED_BACK,
        comparison_baseline_digest=D,
        comparable=True,
        identity_mismatches=[],
        gates=[GateEvidence(gate_id="cpu-execution-success", metric="cpu.success",
                            actual=True, passed=True, reason="ok")],
        improvements={PRIMARY: _improvement(lower=0.01)},
        feasible=True,
        accepted=False,
    )
    payload.update(overrides)
    return CandidateEvaluation(**payload)


class TestJudge:
    def test_accepts_when_lcb_above_mde(self):
        verdict = evaluate_candidate(_candidate(), primary_metric=PRIMARY, minimum_effect=0.0)
        assert verdict.comparable and verdict.feasible and verdict.accepted
        assert any(reason.startswith("S7") for reason in verdict.reasons)

    def test_rejects_when_lcb_below_mde_with_reason(self):
        verdict = evaluate_candidate(
            _candidate(improvements={PRIMARY: _improvement(lower=-0.002)}),
            primary_metric=PRIMARY,
            minimum_effect=0.0,
        )
        assert not verdict.accepted
        assert any("LCB=-0.002000" in reason for reason in verdict.reasons)

    def test_identity_mismatch_short_circuits_with_s0_reason(self):
        verdict = evaluate_candidate(
            _candidate(comparable=False, identity_mismatches=["measurement-missing"]),
            primary_metric=PRIMARY,
            minimum_effect=0.0,
        )
        assert not verdict.comparable and not verdict.accepted
        assert verdict.reasons[0].startswith("S0")

    def test_failed_hard_gate_blocks_even_with_positive_lcb(self):
        verdict = evaluate_candidate(
            _candidate(
                feasible=False,
                gates=[GateEvidence(gate_id="cpu-execution-success", metric="cpu.success",
                                    actual=False, passed=False, reason="crashed")],
            ),
            primary_metric=PRIMARY,
            minimum_effect=0.0,
        )
        assert not verdict.feasible and not verdict.accepted
        assert verdict.reasons[0].startswith("S2")

    def test_missing_primary_improvement_is_not_accepted(self):
        verdict = evaluate_candidate(
            _candidate(improvements={}),
            primary_metric=PRIMARY,
            minimum_effect=0.0,
        )
        assert verdict.feasible and not verdict.accepted
        assert any(reason.startswith("S7") and "no improvement evidence" in reason
                   for reason in verdict.reasons)


def _score(component: str, pressure: float, adverse: float) -> ComponentScore:
    return ComponentScore(
        component=component,
        max_pressure=pressure,
        max_adverse_change=adverse,
        best_pareto_rank=None,
        metric_count=1,
        priorities_digest=D,
    )


class TestScorer:
    def test_components_sort_by_pressure_then_adverse(self):
        scores = score_components(
            [
                DiagnosticPriority(metric_id="m1", component="cpu", pressure=0.9,
                                   adverse_change=0.1, persistence=0.5, confidence=0.5),
                DiagnosticPriority(metric_id="m2", component="memory", pressure=0.4,
                                   adverse_change=0.8, persistence=0.5, confidence=0.5),
                DiagnosticPriority(metric_id="m3", component="cpu", pressure=0.2,
                                   adverse_change=0.95, persistence=0.5, confidence=0.5),
            ]
        )
        assert rank_components(scores) == ["cpu", "memory"]
        cpu = scores[0]
        assert cpu.max_pressure == pytest.approx(0.9)
        assert cpu.max_adverse_change == pytest.approx(0.95)
        assert cpu.metric_count == 2

    def test_empty_priorities_rejected(self):
        with pytest.raises(ValueError, match="at least one"):
            score_components([])


class TestScheduler:
    def _entry_for(self, parameters: dict) -> NegativeCacheEntry:
        from looper_core.system_opt.negative_cache import NegativeCacheIdentity

        return NegativeCacheEntry(
            identity=NegativeCacheIdentity(
                environment_digest=ENV,
                candidate_parameters_digest=candidate_parameters_digest(parameters),
                pressure_protocol_digest=PROTOCOL_CPU,
                formula_versions_digest=formula_versions_digest(FORMULAS),
            ),
            metric_id=PRIMARY,
            verdict=NegativeVerdict.NO_IMPROVEMENT_LCB,
            evidence_digests=[D],
            detail="LCB95 <= MDE",
            recorded_at=FIXED_AT,
        )

    def test_selects_highest_priority_component_candidate(self):
        decision = select_next_candidate(
            [_score("cpu", 0.9, 0.1), _score("memory", 0.4, 0.8)],
            {"cpu": [{"k": "performance"}], "memory": [{"k": "always"}]},
            NegativeCache(),
            environment_digest=ENV,
            pressure_protocol_digests={"cpu": PROTOCOL_CPU, "memory": PROTOCOL_MEMORY},
            formula_versions=FORMULAS,
        )
        assert decision.selection is not None
        assert decision.selection.component == "cpu"
        assert decision.skipped == []

    def test_cache_hit_skips_to_next_candidate_and_records_key(self):
        cached_params = {"k": "performance"}
        cache = NegativeCache([self._entry_for(cached_params)])
        decision = select_next_candidate(
            [_score("cpu", 0.9, 0.1)],
            {"cpu": [cached_params, {"k": "powersave"}]},
            cache,
            environment_digest=ENV,
            pressure_protocol_digests={"cpu": PROTOCOL_CPU},
            formula_versions=FORMULAS,
        )
        assert decision.selection is not None
        assert decision.selection.parameters == {"k": "powersave"}
        assert len(decision.skipped) == 1
        assert decision.skipped[0].component == "cpu"
        assert decision.skipped[0].cache_key

    def test_all_candidates_cached_returns_explicit_exhaustion(self):
        cached_params = {"k": "performance"}
        cache = NegativeCache([self._entry_for(cached_params)])
        decision = select_next_candidate(
            [_score("cpu", 0.9, 0.1)],
            {"cpu": [cached_params]},
            cache,
            environment_digest=ENV,
            pressure_protocol_digests={"cpu": PROTOCOL_CPU},
            formula_versions=FORMULAS,
        )
        assert decision.selection is None
        assert decision.exhausted_reason is not None
        assert "negative cache" in decision.exhausted_reason

    def test_requires_component_scores(self):
        with pytest.raises(ValueError, match="component scores"):
            select_next_candidate(
                [],
                {"cpu": [{"k": "v"}]},
                NegativeCache(),
                environment_digest=ENV,
                pressure_protocol_digests={"cpu": PROTOCOL_CPU},
                formula_versions=FORMULAS,
            )
