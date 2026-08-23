from __future__ import annotations

import pytest

from looper_core.system_opt.component import CandidateSuggestion
from looper_core.system_opt.component.mapping import (
    CandidateRule,
    ConditionOperator,
    EvidenceCondition,
    RuleRejection,
    StrategyFormulaMapping,
    validate_suggestions_in_domain,
)
from looper_core.system_opt.collector import (
    CollectedMetric,
    ComponentMetricSnapshot,
    MetricAvailability,
)
from looper_core.system_opt.scoring import MeasurementBatch, MetricEvidence
from datetime import UTC, datetime

FIXED_AT = datetime(2026, 8, 23, 12, 0, 0, tzinfo=UTC)


def _rule(rule_id="r-1", priority=1, when=None, parameters=None, formula="F-RULE/v0"):
    return CandidateRule(
        rule_id=rule_id,
        when=when or [EvidenceCondition(metric_id="cpu.load", operator=ConditionOperator.GT, threshold=0.5)],
        suggest_parameters=parameters or {"system.governor": "performance"},
        rationale="high load suggests the performance governor",
        formula_id=formula,
        priority=priority,
    )


def _batch(values: dict[str, list[float]]) -> MeasurementBatch:
    return MeasurementBatch(
        identity={"target": "t", "workload": "w", "phase": "p", "tool": "x", "statistics": "s"},
        metrics={name: MetricEvidence(metric_id=name, values=vals) for name, vals in values.items()},
        gate_values={},
    )


def _snapshot(values: dict[str, float]) -> ComponentMetricSnapshot:
    return ComponentMetricSnapshot(
        component="cpu",
        target_id="t",
        environment_digest="sha256:" + "a" * 64,
        collected_at=FIXED_AT,
        metrics={
            name: CollectedMetric(name=name, unit="unit", value=value,
                                  availability=MetricAvailability.READABLE, source="/proc/x")
            for name, value in values.items()
        },
        counting_basis="test",
    )


class TestRuleEvaluation:
    def test_condition_hit_produces_suggestion_in_priority_order(self):
        mapping = StrategyFormulaMapping([_rule(priority=2), _rule(rule_id="r-0", priority=1,
                                                                  parameters={"system.governor": "schedutil"})])
        suggestions = mapping.suggest(None, _batch({"cpu.load": [0.9]}))
        assert [s.parameters["system.governor"] for s in suggestions] == ["schedutil", "performance"]

    def test_condition_miss_records_rejection_reason(self):
        mapping = StrategyFormulaMapping([_rule()])
        suggestions = mapping.suggest(None, _batch({"cpu.load": [0.1]}))
        assert suggestions == []
        assert mapping.last_rejections[0].reason == "cpu.load=0.1 fails gt 0.5"

    def test_missing_metric_is_fail_closed_not_guess(self):
        mapping = StrategyFormulaMapping([_rule()])
        suggestions = mapping.suggest(None, _batch({"other.metric": [1.0]}))
        assert suggestions == []
        assert "missing" in mapping.last_rejections[0].reason

    def test_unavailable_collector_metric_does_not_fire(self):
        snapshot = ComponentMetricSnapshot(
            component="cpu", target_id="t", environment_digest="sha256:" + "a" * 64,
            collected_at=FIXED_AT,
            metrics={"cpu.load": CollectedMetric(name="cpu.load", unit="unit", value=None,
                                                 availability=MetricAvailability.UNAVAILABLE,
                                                 unavailable_reason="hidden in guest", source="/sys/x")},
            counting_basis="test",
        )
        mapping = StrategyFormulaMapping([_rule()])
        assert mapping.suggest(snapshot, None) == []
        assert "missing" in mapping.last_rejections[0].reason

    def test_batch_takes_precedence_over_snapshot(self):
        mapping = StrategyFormulaMapping([_rule()])
        suggestions = mapping.suggest(_snapshot({"cpu.load": 0.9}), _batch({"cpu.load": [0.1]}))
        assert suggestions == []  # batch median 0.1 fails the condition

    def test_duplicate_priorities_rejected(self):
        with pytest.raises(ValueError, match="unique"):
            StrategyFormulaMapping([_rule(), _rule(rule_id="r-2")])

    def test_no_evidence_rejected(self):
        with pytest.raises(ValueError, match="evidence"):
            StrategyFormulaMapping([_rule()]).suggest(None, None)


class TestDomainValidation:
    def _domains(self):
        class FakeDomain:
            def to_search_parameter(self):
                class P:
                    choices = ["performance", "schedutil"]
                return P()
        return {"system.governor": FakeDomain()}

    def test_in_domain_suggestion_passes(self):
        suggestions = [CandidateSuggestion(parameters={"system.governor": "performance"},
                                           rationale="r", formula_id="F/v0")]
        accepted, rejected = validate_suggestions_in_domain(suggestions, self._domains())
        assert accepted and not rejected

    def test_out_of_domain_is_rejected_not_corrected(self):
        suggestions = [CandidateSuggestion(parameters={"system.governor": "powersave"},
                                           rationale="r", formula_id="F/v0")]
        accepted, rejected = validate_suggestions_in_domain(suggestions, self._domains())
        assert not accepted
        assert "outside authorized choices" in rejected[0].reason

    def test_unknown_parameter_is_rejected(self):
        suggestions = [CandidateSuggestion(parameters={"system.unknown": "x"},
                                           rationale="r", formula_id="F/v0")]
        accepted, rejected = validate_suggestions_in_domain(suggestions, self._domains())
        assert not accepted
        assert "not in the resolved search space" in rejected[0].reason

    def test_rejection_round_trips(self):
        rejection = RuleRejection(rule_id="r", reason="x")
        assert RuleRejection.model_validate_json(rejection.model_dump_json()) == rejection
