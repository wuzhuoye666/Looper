"""L5 公式映射逻辑：策略规则 × 证据 → 带优先级的域内候选建议。

架构层：L5（docs/system-optimizer/architecture/overall.md）。逻辑保证（本模块
最重要的职责）：

1. 规则只在**全部条件命中**时触发；任一条件引用的指标在证据中缺失 →
   该规则不触发并记录原因（fail-closed，不猜）；
2. 建议参数必须落在组件已解析的合法域内，越域建议被拒绝并记录，
   不静默丢弃也不静默修正；
3. 映射只**排序与建议**，永不判定接受——门禁与终裁在 L8；
4. 输出确定性：同策略同证据同域 → 同摘要。
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any

from pydantic import Field, model_validator

from looper_core.canonical import canonical_digest
from looper_core.contracts import StrictModel
from looper_core.system_opt.collector import ComponentMetricSnapshot
from looper_core.system_opt.component import CandidateSuggestion
from looper_core.system_opt.scoring import MeasurementBatch

from statistics import median


class ConditionOperator(StrEnum):
    LT = "lt"
    LTE = "lte"
    GT = "gt"
    GTE = "gte"


class EvidenceCondition(StrictModel):
    metric_id: str = Field(min_length=1, max_length=160)
    operator: ConditionOperator
    threshold: float

    @model_validator(mode="after")
    def threshold_finite(self) -> EvidenceCondition:
        if self.threshold != self.threshold or self.threshold in (float("inf"), float("-inf")):
            raise ValueError("threshold must be finite")
        return self


class CandidateRule(StrictModel):
    rule_id: str = Field(min_length=1, max_length=120, pattern=r"^[a-z][a-z0-9.-]*$")
    when: list[EvidenceCondition] = Field(min_length=1)
    suggest_parameters: dict[str, Any] = Field(min_length=1)
    rationale: str = Field(min_length=1, max_length=500)
    formula_id: str = Field(min_length=1, max_length=200)
    priority: int = Field(ge=1, le=1000)

    @model_validator(mode="after")
    def unique_metrics(self) -> CandidateRule:
        metric_ids = [condition.metric_id for condition in self.when]
        if len(metric_ids) != len(set(metric_ids)):
            raise ValueError("a rule cannot test the same metric twice")
        return self

    @property
    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json"))


class RuleRejection(StrictModel):
    rule_id: str = Field(min_length=1)
    reason: str = Field(min_length=1, max_length=500)


class StrategyFormulaMapping:
    """Evaluate strategy rules against evidence; validate suggestions in-domain."""

    def __init__(self, rules: list[CandidateRule]) -> None:
        priorities = [rule.priority for rule in rules]
        if len(priorities) != len(set(priorities)):
            raise ValueError("rule priorities must be unique for a deterministic order")
        self._rules = sorted(rules, key=lambda rule: (rule.priority, rule.rule_id))

    def _condition_holds(self, condition: EvidenceCondition, value: float) -> bool:
        if condition.operator is ConditionOperator.LT:
            return value < condition.threshold
        if condition.operator is ConditionOperator.LTE:
            return value <= condition.threshold
        if condition.operator is ConditionOperator.GT:
            return value > condition.threshold
        return value >= condition.threshold

    def _aggregate(
        self,
        batch: MeasurementBatch | None,
        snapshot: ComponentMetricSnapshot | None,
        metric_id: str,
    ) -> float | None:
        # Evidence preference: measured distribution first, collector snapshot second.
        if batch is not None and metric_id in batch.metrics:
            values = batch.metrics[metric_id].values
            if not values:
                return None
            return float(median(values))
        if snapshot is not None and metric_id in snapshot.metrics:
            metric = snapshot.metrics[metric_id]
            if metric.value is None:
                return None
            return float(metric.value)
        return None

    def suggest(
        self,
        snapshot: ComponentMetricSnapshot | None,
        baseline: MeasurementBatch | None,
    ) -> list[CandidateSuggestion]:
        if baseline is None and snapshot is None:
            raise ValueError("formula mapping requires at least one evidence source")
        suggestions: list[CandidateSuggestion] = []
        for rule in self._rules:
            reasons: list[str] = []
            for condition in rule.when:
                value = self._aggregate(baseline, snapshot, condition.metric_id)
                if value is None:
                    reasons.append(f"metric '{condition.metric_id}' missing or empty in evidence")
                    break
                if not self._condition_holds(condition, value):
                    reasons.append(
                        f"{condition.metric_id}={value} fails {condition.operator.value} "
                        f"{condition.threshold}"
                    )
                    break
            if not reasons:
                suggestions.append(
                    CandidateSuggestion(
                        parameters=dict(rule.suggest_parameters),
                        rationale=rule.rationale,
                        formula_id=rule.formula_id,
                    )
                )
        self.last_rejections = [
            RuleRejection(rule_id=rule.rule_id, reason=reason)
            for rule in self._rules
            for reason in [self._first_failure(rule, baseline, snapshot)]
            if reason is not None
        ]
        return suggestions

    def _first_failure(
        self,
        rule: CandidateRule,
        baseline: MeasurementBatch | None,
        snapshot: ComponentMetricSnapshot | None,
    ) -> str | None:
        for condition in rule.when:
            value = self._aggregate(baseline, snapshot, condition.metric_id)
            if value is None:
                return f"metric '{condition.metric_id}' missing or empty in evidence"
            if not self._condition_holds(condition, value):
                return (
                    f"{condition.metric_id}={value} fails {condition.operator.value} "
                    f"{condition.threshold}"
                )
        return None


def validate_suggestions_in_domain(
    suggestions: list[CandidateSuggestion],
    domains: Mapping[str, Any],
) -> tuple[list[CandidateSuggestion], list[RuleRejection]]:
    """Split suggestions into in-domain and rejected (recorded, never dropped silently)."""

    accepted: list[CandidateSuggestion] = []
    rejected: list[RuleRejection] = []
    for suggestion in suggestions:
        problems: list[str] = []
        for name, value in suggestion.parameters.items():
            if name not in domains:
                problems.append(f"parameter '{name}' is not in the resolved search space")
                continue
            parameter = domains[name].to_search_parameter() if hasattr(domains[name], "to_search_parameter") else None
            choices = getattr(parameter, "choices", None) if parameter is not None else None
            if choices is not None and value not in choices:
                problems.append(f"value {value!r} outside authorized choices {choices}")
        if problems:
            rejected.append(
                RuleRejection(rule_id=suggestion.formula_id, reason="; ".join(problems))
            )
        else:
            accepted.append(suggestion)
    return accepted, rejected


__all__ = [
    "CandidateRule",
    "ConditionOperator",
    "EvidenceCondition",
    "RuleRejection",
    "StrategyFormulaMapping",
    "validate_suggestions_in_domain",
]
