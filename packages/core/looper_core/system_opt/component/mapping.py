"""L5 公式映射逻辑：策略规则 × 证据 → 带优先级的域内候选建议。

架构层：L5（docs/system-optimizer/architecture/overall.md）。逻辑保证：

1. 条件可声明分布统计量（median/mean/p95/cv）与置信模式
   （point / lcb95 / ucb95，bootstrap 界，formula id
   F-PROJECT-CONDITION-BOOTSTRAP/v1）——恢复 S1.1/S4/S7 原有的
   分布与置信纪律，不允许只看中位数点估计；
2. 样本数低于规则的 minimum_samples、或置信模式/分布统计量缺乏
   分布证据（collector 快照只有点值）→ 条件"未决"，规则不触发并记录
   （fail-closed，不猜）；
3. 全部条件命中才触发；建议参数必须落在已解析合法域内，越域拒绝并记录；
4. 映射只排序与建议，永不终裁（L8 职责）；同策略同证据同域同种子 → 同输出。
"""

from __future__ import annotations

import random
from collections.abc import Mapping
from enum import StrEnum
from statistics import fmean, median
from typing import Any

from pydantic import Field, model_validator

from looper_core.analysis import quantile
from looper_core.canonical import canonical_digest
from looper_core.contracts import StrictModel
from looper_core.system_opt.collector import ComponentMetricSnapshot
from looper_core.system_opt.component import CandidateSuggestion
from looper_core.system_opt.scoring import MeasurementBatch

CONDITION_BOOTSTRAP_FORMULA = "F-PROJECT-CONDITION-BOOTSTRAP/v2"


class ConditionOperator(StrEnum):
    LT = "lt"
    LTE = "lte"
    GT = "gt"
    GTE = "gte"


class ConditionStatistic(StrEnum):
    MEDIAN = "median"
    MEAN = "mean"
    P95 = "p95"
    CV = "cv"


class ConfidenceMode(StrEnum):
    POINT = "point"
    LCB95 = "lcb95"
    UCB95 = "ucb95"


def _statistic(values: list[float], statistic: ConditionStatistic) -> float:
    if statistic is ConditionStatistic.MEDIAN:
        return float(median(values))
    if statistic is ConditionStatistic.MEAN:
        return float(fmean(values))
    if statistic is ConditionStatistic.P95:
        return quantile(values, 0.95)
    if len(values) < 2:
        raise ValueError("cv requires at least two samples")
    mean = fmean(values)
    if mean == 0:
        raise ValueError("cv is undefined for a zero-mean metric")
    variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
    return variance ** 0.5 / abs(mean)


def _bootstrap_bound(
    values: list[float],
    statistic: ConditionStatistic,
    mode: ConfidenceMode,
    seed: int,
    resamples: int,
) -> float:
    generator = random.Random(seed)
    bounds: list[float] = []
    for _ in range(resamples):
        sample = [values[generator.randrange(len(values))] for _ in values]
        bounds.append(_statistic(sample, statistic))
    if mode is ConfidenceMode.LCB95:
        return quantile(bounds, 0.05)
    return quantile(bounds, 0.95)


class EvidenceCondition(StrictModel):
    metric_id: str = Field(min_length=1, max_length=160)
    operator: ConditionOperator
    threshold: float
    statistic: ConditionStatistic = ConditionStatistic.MEDIAN
    confidence: ConfidenceMode = ConfidenceMode.POINT
    minimum_samples: int = Field(default=1, ge=1, le=10000)

    @model_validator(mode="after")
    def threshold_finite(self) -> EvidenceCondition:
        if self.threshold != self.threshold or self.threshold in (
            float("inf"),
            float("-inf"),
        ):
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


class _Undecided(Exception):
    pass


class _NotMet(Exception):
    pass


class StrategyFormulaMapping:
    """Evaluate strategy rules against evidence; validate suggestions in-domain."""

    def __init__(
        self,
        rules: list[CandidateRule],
        *,
        bootstrap_seed: int = 20260823,
        bootstrap_resamples: int = 2000,
    ) -> None:
        priorities = [rule.priority for rule in rules]
        if len(priorities) != len(set(priorities)):
            raise ValueError("rule priorities must be unique for a deterministic order")
        self._rules = sorted(rules, key=lambda rule: (rule.priority, rule.rule_id))
        self._seed = bootstrap_seed
        self._resamples = bootstrap_resamples
        self.last_rejections: list[RuleRejection] = []

    def _compare(self, value: float, condition: EvidenceCondition) -> bool:
        if condition.operator is ConditionOperator.LT:
            return value < condition.threshold
        if condition.operator is ConditionOperator.LTE:
            return value <= condition.threshold
        if condition.operator is ConditionOperator.GT:
            return value > condition.threshold
        return value >= condition.threshold

    def _evaluate(
        self,
        condition: EvidenceCondition,
        baseline: MeasurementBatch | None,
        snapshot: ComponentMetricSnapshot | None,
    ) -> bool:
        if baseline is not None and condition.metric_id in baseline.metrics:
            values = baseline.metrics[condition.metric_id].values
            if len(values) < condition.minimum_samples:
                raise _Undecided(
                    f"metric '{condition.metric_id}' has {len(values)} samples, "
                    f"below minimum_samples={condition.minimum_samples}"
                )
            statistic = _statistic(values, condition.statistic)
            if condition.confidence is ConfidenceMode.POINT:
                if not self._compare(statistic, condition):
                    raise _NotMet(
                        f"{condition.metric_id}={statistic} fails "
                        f"{condition.operator.value} {condition.threshold}"
                    )
                return True
            bound = _bootstrap_bound(
                values, condition.statistic, condition.confidence, self._seed, self._resamples
            )
            if not self._compare(bound, condition):
                raise _NotMet(
                    f"{condition.metric_id} {condition.confidence.value}={bound} fails "
                    f"{condition.operator.value} {condition.threshold}"
                )
            return True
        if snapshot is not None and condition.metric_id in snapshot.metrics:
            metric = snapshot.metrics[condition.metric_id]
            if metric.value is None:
                raise _Undecided(
                    f"metric '{condition.metric_id}' is unavailable in the snapshot"
                )
            if condition.confidence is not ConfidenceMode.POINT:
                raise _Undecided(
                    f"metric '{condition.metric_id}': confidence mode "
                    f"{condition.confidence.value} requires a measurement batch; "
                    "the snapshot carries a single point value"
                )
            if condition.statistic is not ConditionStatistic.MEDIAN:
                raise _Undecided(
                    f"metric '{condition.metric_id}': snapshot supports median only, "
                    f"not {condition.statistic.value}"
                )
            value = float(metric.value)
            if not self._compare(value, condition):
                raise _NotMet(
                    f"{condition.metric_id}={value} fails "
                    f"{condition.operator.value} {condition.threshold}"
                )
            return True
        raise _Undecided(f"metric '{condition.metric_id}' missing in evidence")

    def suggest(
        self,
        snapshot: ComponentMetricSnapshot | None,
        baseline: MeasurementBatch | None,
    ) -> list[CandidateSuggestion]:
        if baseline is None and snapshot is None:
            raise ValueError("formula mapping requires at least one evidence source")
        suggestions: list[CandidateSuggestion] = []
        rejections: list[RuleRejection] = []
        for rule in self._rules:
            fired = True
            reason: str | None = None
            for condition in rule.when:
                try:
                    self._evaluate(condition, baseline, snapshot)
                except _NotMet as not_met:
                    reason = str(not_met)
                    fired = False
                    break
                except _Undecided as undecided:
                    reason = f"undecided (fail-closed): {undecided}"
                    fired = False
                    break
            if fired:
                suggestions.append(
                    CandidateSuggestion(
                        parameters=dict(rule.suggest_parameters),
                        rationale=rule.rationale,
                        formula_id=rule.formula_id,
                    )
                )
            else:
                rejections.append(
                    RuleRejection(rule_id=rule.rule_id, reason=reason or "unknown")
                )
        self.last_rejections = rejections
        return suggestions


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
            parameter = (
                domains[name].to_search_parameter()
                if hasattr(domains[name], "to_search_parameter")
                else None
            )
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
    "CONDITION_BOOTSTRAP_FORMULA",
    "CandidateRule",
    "ConditionOperator",
    "ConditionStatistic",
    "ConfidenceMode",
    "EvidenceCondition",
    "RuleRejection",
    "StrategyFormulaMapping",
    "validate_suggestions_in_domain",
]
