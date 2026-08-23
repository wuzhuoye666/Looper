from __future__ import annotations

import math
import random
from collections.abc import Mapping, Sequence
from statistics import mean

from pydantic import Field

from looper_core.analysis import InsufficientEvidence, aggregate, quantile
from looper_core.canonical import canonical_digest, canonical_json
from looper_core.contracts import Operator, StrictModel
from looper_core.system_opt.policy import (
    HardGateContract,
    MetricContract,
    MetricDirection,
    PressureMethod,
    StatisticsPolicy,
)


class MetricEvidence(StrictModel):
    metric_id: str
    values: list[float] = Field(min_length=1)

    @property
    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json"))


class MeasurementPhaseEvidence(StrictModel):
    phase_id: str
    kind: str
    command_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    status: str
    elapsed_seconds: float = Field(ge=0)


class MeasurementStabilityEvidence(StrictModel):
    metric_id: str
    statistic: str
    formula_id: str
    sample_count: int = Field(ge=2)
    value: float = Field(ge=0)
    enforcement: str
    acceptance_limit: float | None = Field(default=None, gt=0)
    accepted: bool | None


class MeasurementBatch(StrictModel):
    identity: dict[str, str]
    metrics: dict[str, MetricEvidence]
    gate_values: dict[str, float | bool | None]
    pressure_protocol_digest: str | None = Field(
        default=None, pattern=r"^sha256:[0-9a-f]{64}$"
    )
    phase_evidence: list[MeasurementPhaseEvidence] = Field(default_factory=list)
    stability_evidence: MeasurementStabilityEvidence | None = None

    @property
    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json", exclude_none=False))


class ImprovementEvidence(StrictModel):
    metric_id: str
    formula_id: str
    baseline_digest: str
    candidate_digest: str
    baseline_estimate: float
    candidate_estimate: float
    estimate: float
    lower: float
    upper: float
    minimum_effect: float
    accepted: bool


class GateEvidence(StrictModel):
    gate_id: str
    metric: str
    actual: float | bool | None
    passed: bool
    reason: str


class DiagnosticPriority(StrictModel):
    metric_id: str
    component: str
    pressure: float
    adverse_change: float
    persistence: float = Field(ge=0, le=1)
    confidence: float = Field(ge=0, le=1)
    pareto_rank: int | None = None


def comparable(
    baseline: Mapping[str, str],
    candidate: Mapping[str, str],
    required_fields: Sequence[str],
) -> tuple[bool, list[str]]:
    mismatches = [
        field
        for field in required_fields
        if field not in baseline
        or field not in candidate
        or canonical_json(baseline[field]) != canonical_json(candidate[field])
    ]
    return not mismatches, mismatches


def _distance(value: float, contract: MetricContract) -> float:
    if contract.direction == MetricDirection.TARGET:
        if contract.target is None:
            raise InsufficientEvidence("target metric requires an explicit target")
        return abs(value - contract.target)
    if contract.direction == MetricDirection.RANGE:
        if contract.lower_bound is None or contract.upper_bound is None:
            raise InsufficientEvidence("range metric requires lower_bound and upper_bound")
        if value < contract.lower_bound:
            return contract.lower_bound - value
        if value > contract.upper_bound:
            return value - contract.upper_bound
        return 0.0
    raise ValueError("distance is only defined for target/range metrics")


def improvement_value(candidate: float, baseline: float, contract: MetricContract) -> float:
    if contract.direction == MetricDirection.MAXIMIZE:
        if contract.scale is None:
            raise InsufficientEvidence(
                f"{contract.id} maximize metric requires an explicit scale"
            )
        return (candidate - baseline) / contract.scale
    if contract.direction == MetricDirection.MINIMIZE:
        if contract.scale is None:
            raise InsufficientEvidence(
                f"{contract.id} minimize metric requires an explicit scale"
            )
        return (baseline - candidate) / contract.scale
    if contract.direction in {MetricDirection.TARGET, MetricDirection.RANGE}:
        scale = contract.scale or 1.0
        return (_distance(baseline, contract) - _distance(candidate, contract)) / scale
    raise InsufficientEvidence("diagnostic-only metrics do not have candidate utility")


def bootstrap_improvement(
    candidate: MetricEvidence,
    baseline: MetricEvidence,
    contract: MetricContract,
    statistics: StatisticsPolicy,
) -> ImprovementEvidence:
    if len(candidate.values) < contract.minimum_samples:
        raise InsufficientEvidence(f"{contract.id} candidate samples are insufficient")
    if len(baseline.values) < contract.minimum_samples:
        raise InsufficientEvidence(f"{contract.id} baseline samples are insufficient")
    if contract.minimum_effect is None:
        raise InsufficientEvidence(f"{contract.id} minimum_effect is missing")
    baseline_estimate = aggregate(baseline.values, contract.aggregation)
    candidate_estimate = aggregate(candidate.values, contract.aggregation)
    point = improvement_value(candidate_estimate, baseline_estimate, contract)
    generator = random.Random(statistics.random_seed)
    estimates: list[float] = []
    for _ in range(statistics.bootstrap_resamples):
        candidate_sample = [generator.choice(candidate.values) for _ in candidate.values]
        baseline_sample = [generator.choice(baseline.values) for _ in baseline.values]
        estimates.append(
            improvement_value(
                aggregate(candidate_sample, contract.aggregation),
                aggregate(baseline_sample, contract.aggregation),
                contract,
            )
        )
    alpha = (1 - statistics.confidence_level) / 2
    lower = quantile(estimates, alpha)
    upper = quantile(estimates, 1 - alpha)
    return ImprovementEvidence(
        metric_id=contract.id,
        formula_id="F-PROJECT-S6-S7/v1alpha1",
        baseline_digest=baseline.digest,
        candidate_digest=candidate.digest,
        baseline_estimate=baseline_estimate,
        candidate_estimate=candidate_estimate,
        estimate=point,
        lower=lower,
        upper=upper,
        minimum_effect=contract.minimum_effect,
        accepted=lower > contract.minimum_effect,
    )


def pressure_value(value: float, contract: MetricContract) -> float:
    reference = contract.pressure_reference
    if contract.pressure_method == PressureMethod.EXPLICIT_SCORE:
        if not 0 <= value <= 1:
            raise InsufficientEvidence("explicit pressure score must be in [0, 1]")
        return value
    if contract.pressure_method == PressureMethod.NONE or reference is None:
        raise InsufficientEvidence(f"{contract.id} has no pressure transform")
    if contract.pressure_method == PressureMethod.UTILIZATION:
        if reference <= 0:
            raise InsufficientEvidence("utilization capacity must be positive")
        return max(0.0, value / reference)
    if contract.pressure_method == PressureMethod.UPPER_LIMIT_EXCESS:
        scale = contract.scale or abs(reference)
        if scale == 0:
            raise InsufficientEvidence("upper-limit pressure scale is zero")
        return max(0.0, value - reference) / scale
    if contract.pressure_method == PressureMethod.LOWER_LIMIT_DEFICIT:
        scale = contract.scale or abs(reference)
        if scale == 0:
            raise InsufficientEvidence("lower-limit pressure scale is zero")
        return max(0.0, reference - value) / scale
    if contract.pressure_method == PressureMethod.TARGET_DISTANCE:
        scale = contract.scale or max(abs(reference), 1.0)
        return abs(value - reference) / scale
    if contract.pressure_method == PressureMethod.RANGE_EXCESS:
        if contract.lower_bound is None or contract.upper_bound is None:
            raise InsufficientEvidence("range pressure requires metric bounds")
        return _distance(value, contract) / (contract.scale or 1.0)
    raise InsufficientEvidence(f"unsupported pressure method {contract.pressure_method}")


def adverse_change(current: float, baseline: float, contract: MetricContract) -> float:
    if contract.direction == MetricDirection.MINIMIZE:
        if baseline == 0:
            if contract.scale is None:
                raise InsufficientEvidence("near-zero baseline requires an explicit scale")
            return (current - baseline) / contract.scale
        return (current - baseline) / abs(baseline)
    if contract.direction == MetricDirection.MAXIMIZE:
        if baseline == 0:
            if contract.scale is None:
                raise InsufficientEvidence("near-zero baseline requires an explicit scale")
            return (baseline - current) / contract.scale
        return (baseline - current) / abs(baseline)
    if contract.direction in {MetricDirection.TARGET, MetricDirection.RANGE}:
        return _distance(current, contract) - _distance(baseline, contract)
    if contract.scale is None:
        raise InsufficientEvidence("diagnostic-only adverse change requires an explicit scale")
    return (current - baseline) / contract.scale


def _gate_matches(actual: float | bool | None, gate: HardGateContract) -> bool:
    if gate.operator == Operator.TRUE:
        return actual is True
    if gate.operator == Operator.FALSE:
        return actual is False
    if actual is None or gate.threshold is None or isinstance(actual, bool):
        return False
    left = float(actual)
    right = float(gate.threshold)
    return {
        Operator.EQ: left == right,
        Operator.NE: left != right,
        Operator.LT: left < right,
        Operator.LTE: left <= right,
        Operator.GT: left > right,
        Operator.GTE: left >= right,
    }[gate.operator]


def evaluate_hard_gates(
    gates: Sequence[HardGateContract], values: Mapping[str, float | bool | None]
) -> list[GateEvidence]:
    return [
        GateEvidence(
            gate_id=gate.id,
            metric=gate.metric,
            actual=values.get(gate.metric),
            passed=_gate_matches(values.get(gate.metric), gate),
            reason=gate.reason,
        )
        for gate in gates
    ]


def diagnostic_priorities(
    current: MeasurementBatch,
    reference: MeasurementBatch,
    contracts: Sequence[MetricContract],
) -> list[DiagnosticPriority]:
    priorities: list[DiagnosticPriority] = []
    for contract in contracts:
        if contract.id not in current.metrics or contract.id not in reference.metrics:
            continue
        current_values = current.metrics[contract.id].values
        reference_values = reference.metrics[contract.id].values
        current_estimate = aggregate(current_values, contract.aggregation)
        reference_estimate = aggregate(reference_values, contract.aggregation)
        adverse_samples = sum(
            adverse_change(value, reference_estimate, contract) > 0 for value in current_values
        )
        priorities.append(
            DiagnosticPriority(
                metric_id=contract.id,
                component=contract.component,
                pressure=pressure_value(current_estimate, contract),
                adverse_change=adverse_change(current_estimate, reference_estimate, contract),
                persistence=adverse_samples / len(current_values),
                confidence=min(1.0, len(current_values) / contract.minimum_samples),
            )
        )

    remaining = list(range(len(priorities)))
    rank = 1
    while remaining:
        front: list[int] = []
        for index in remaining:
            point = priorities[index]
            dominated = any(
                _priority_dominates(priorities[other], point)
                for other in remaining
                if other != index
            )
            if not dominated:
                front.append(index)
        if not front:
            raise RuntimeError("could not resolve diagnostic Pareto front")
        for index in front:
            priorities[index].pareto_rank = rank
        remaining = [index for index in remaining if index not in set(front)]
        rank += 1
    return sorted(
        priorities,
        key=lambda item: (
            item.pareto_rank or math.inf,
            -item.pressure,
            -item.adverse_change,
            -item.persistence,
            -item.confidence,
            item.metric_id,
        ),
    )


def _priority_dominates(left: DiagnosticPriority, right: DiagnosticPriority) -> bool:
    left_values = (left.pressure, left.adverse_change, left.persistence, left.confidence)
    right_values = (right.pressure, right.adverse_change, right.persistence, right.confidence)
    no_worse = all(left >= right for left, right in zip(left_values, right_values, strict=True))
    strictly_better = any(
        left > right for left, right in zip(left_values, right_values, strict=True)
    )
    return no_worse and strictly_better


def evidence_coverage(expected_metric_ids: Sequence[str], batch: MeasurementBatch) -> float:
    if not expected_metric_ids:
        return 0.0
    available = sum(metric_id in batch.metrics for metric_id in expected_metric_ids)
    return available / len(expected_metric_ids)


def summarize_priority_by_component(
    priorities: Sequence[DiagnosticPriority],
) -> dict[str, dict[str, float | int]]:
    grouped: dict[str, list[DiagnosticPriority]] = {}
    for priority in priorities:
        grouped.setdefault(priority.component, []).append(priority)
    return {
        component: {
            "best_pareto_rank": min(item.pareto_rank or math.inf for item in items),
            "max_pressure": max(item.pressure for item in items),
            "max_adverse_change": max(item.adverse_change for item in items),
            "mean_persistence": mean(item.persistence for item in items),
        }
        for component, items in sorted(grouped.items())
    }


__all__ = [
    "DiagnosticPriority",
    "GateEvidence",
    "ImprovementEvidence",
    "MeasurementBatch",
    "MetricEvidence",
    "adverse_change",
    "bootstrap_improvement",
    "comparable",
    "diagnostic_priorities",
    "evaluate_hard_gates",
    "evidence_coverage",
    "improvement_value",
    "pressure_value",
    "summarize_priority_by_component",
]
