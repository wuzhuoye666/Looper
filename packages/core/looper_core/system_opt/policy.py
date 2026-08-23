from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

import yaml
from pydantic import Field, field_validator, model_validator

from looper_core.contracts import Aggregation, Operator, StrictModel

OPTIMIZATION_POLICY_SCHEMA = "looper.system-optimization-policy/v1alpha1"


class PolicyError(ValueError):
    pass


class OptimizationMode(StrEnum):
    GENERAL = "general"
    WORKLOAD = "workload"


class MetricRole(StrEnum):
    BUSINESS_PRIMARY = "business-primary"
    BUSINESS_SECONDARY = "business-secondary"
    COMPONENT_DIAGNOSTIC = "component-diagnostic"
    HARD_GATE = "hard-gate"
    COST = "cost"
    RISK = "risk"


class MetricDirection(StrEnum):
    MAXIMIZE = "maximize"
    MINIMIZE = "minimize"
    TARGET = "target"
    RANGE = "range"
    DIAGNOSTIC_ONLY = "diagnostic-only"


class PressureMethod(StrEnum):
    UTILIZATION = "utilization"
    UPPER_LIMIT_EXCESS = "upper-limit-excess"
    LOWER_LIMIT_DEFICIT = "lower-limit-deficit"
    TARGET_DISTANCE = "target-distance"
    RANGE_EXCESS = "range-excess"
    EXPLICIT_SCORE = "explicit-score"
    NONE = "none"


class MetricContract(StrictModel):
    id: str = Field(min_length=1, max_length=160, pattern=r"^[a-z][a-z0-9._-]*$")
    role: MetricRole
    component: str = Field(min_length=1, max_length=80)
    direction: MetricDirection
    unit: str = Field(min_length=1, max_length=80)
    scope: str = Field(min_length=1, max_length=200)
    phase: str = Field(min_length=1, max_length=120)
    aggregation: Aggregation
    minimum_samples: int = Field(ge=2, le=1000000)
    scale: float | None = Field(default=None, gt=0)
    minimum_effect: float | None = Field(default=None, ge=0)
    target: float | None = None
    lower_bound: float | None = None
    upper_bound: float | None = None
    pressure_method: PressureMethod
    pressure_reference: float | None = None
    source: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def validate_semantics(self) -> MetricContract:
        scored_roles = {
            MetricRole.BUSINESS_PRIMARY,
            MetricRole.BUSINESS_SECONDARY,
            MetricRole.COST,
            MetricRole.RISK,
        }
        if self.role in scored_roles and self.direction == MetricDirection.DIAGNOSTIC_ONLY:
            raise ValueError("scored metrics require a scoring direction")
        if self.role in scored_roles and self.minimum_effect is None:
            raise ValueError("scored metrics require an explicit minimum_effect")
        if (
            self.direction in {MetricDirection.MAXIMIZE, MetricDirection.MINIMIZE}
            and self.scale is None
        ):
            raise ValueError("maximize/minimize metrics require an explicit scale")
        if self.direction == MetricDirection.TARGET and self.target is None:
            raise ValueError("target metrics require target")
        if self.direction == MetricDirection.RANGE:
            if self.lower_bound is None or self.upper_bound is None:
                raise ValueError("range metrics require lower_bound and upper_bound")
            if self.lower_bound > self.upper_bound:
                raise ValueError("metric lower_bound cannot exceed upper_bound")
        if self.pressure_method == PressureMethod.NONE:
            if self.role == MetricRole.COMPONENT_DIAGNOSTIC:
                raise ValueError("component diagnostic metrics require a pressure transform")
        elif (
            self.pressure_method != PressureMethod.EXPLICIT_SCORE
            and self.pressure_reference is None
        ):
            raise ValueError("pressure transform requires pressure_reference")
        return self


class HardGateContract(StrictModel):
    id: str = Field(min_length=1, max_length=160, pattern=r"^[a-z][a-z0-9._-]*$")
    metric: str = Field(min_length=1, max_length=160)
    operator: Operator
    threshold: float | bool | None = None
    reason: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def validate_threshold(self) -> HardGateContract:
        if self.operator not in {Operator.TRUE, Operator.FALSE} and self.threshold is None:
            raise ValueError("comparison hard gates require an explicit threshold")
        return self


class StatisticsPolicy(StrictModel):
    confidence_level: float = Field(gt=0.5, lt=1)
    bootstrap_resamples: int = Field(ge=100, le=100000)
    random_seed: int = Field(ge=0)
    baseline_repeats: int = Field(ge=2, le=10000)
    candidate_repeats: int = Field(ge=2, le=10000)
    baseline_every_n: int = Field(ge=1, le=100000)


class SearchPolicy(StrictModel):
    generator: Literal["grid", "random", "optuna-tpe", "optuna-nsga2"]
    random_seed: int = Field(ge=0)
    max_candidates: int = Field(ge=1, le=100000)
    max_attempts: int = Field(ge=1, le=1000000)
    wall_time_seconds: float = Field(gt=0, le=31536000)
    no_improvement_limit: int = Field(ge=1, le=100000)
    target_improvement: float | None = Field(default=None, ge=0)
    routed_component_limit: int | None = Field(default=None, ge=1)
    tie_break_order: list[
        Literal["primary-lower", "primary-estimate", "fewer-changes", "candidate-id"]
    ] = Field(min_length=1)

    @field_validator("tie_break_order")
    @classmethod
    def unique_tie_breakers(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("tie_break_order must be unique")
        return values


class SafetyExecutionContract(StrictModel):
    max_changes: int = Field(ge=1, le=100)
    max_changes_reason: str | None = Field(default=None, min_length=1, max_length=1000)
    require_privileged: bool
    pinned_items: list[str]
    ownership_unknown_items: list[str]
    high_risk_waivers: list[str]

    @model_validator(mode="after")
    def validate_safety(self) -> SafetyExecutionContract:
        if self.max_changes > 5 and not self.max_changes_reason:
            raise ValueError("max_changes above 5 requires an explicit reason")
        for field_name in (
            "pinned_items",
            "ownership_unknown_items",
            "high_risk_waivers",
        ):
            values = getattr(self, field_name)
            if len(values) != len(set(values)):
                raise ValueError(f"{field_name} must be unique")
        return self


class IdentityPolicy(StrictModel):
    required_fields: list[str] = Field(min_length=1)

    @field_validator("required_fields")
    @classmethod
    def unique_fields(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("identity required_fields must be unique")
        return values


class SystemOptimizationPolicy(StrictModel):
    schema_version: Literal[OPTIMIZATION_POLICY_SCHEMA]
    id: str = Field(min_length=1, max_length=160, pattern=r"^[a-z][a-z0-9.-]*$")
    mode: OptimizationMode
    identity: IdentityPolicy
    statistics: StatisticsPolicy
    search: SearchPolicy
    safety: SafetyExecutionContract
    metrics: list[MetricContract] = Field(min_length=1)
    hard_gates: list[HardGateContract]
    authorized_components: list[str] = Field(min_length=1)
    metadata: dict[str, Any]

    @model_validator(mode="after")
    def validate_policy(self) -> SystemOptimizationPolicy:
        metrics = {metric.id: metric for metric in self.metrics}
        if len(metrics) != len(self.metrics):
            raise ValueError("metric ids must be unique")
        gate_ids = {gate.id for gate in self.hard_gates}
        if len(gate_ids) != len(self.hard_gates):
            raise ValueError("hard gate ids must be unique")
        missing = sorted({gate.metric for gate in self.hard_gates} - set(metrics))
        if missing:
            raise ValueError(f"hard gates reference unknown metrics: {missing}")
        primary = [metric for metric in self.metrics if metric.role == MetricRole.BUSINESS_PRIMARY]
        if len(primary) != 1:
            raise ValueError("exactly one business-primary metric is required")
        diagnostic_count = sum(
            metric.role == MetricRole.COMPONENT_DIAGNOSTIC for metric in self.metrics
        )
        if self.mode == OptimizationMode.WORKLOAD and diagnostic_count == 0:
            raise ValueError("workload mode requires component-diagnostic metrics")
        if self.mode == OptimizationMode.WORKLOAD and self.search.routed_component_limit is None:
            raise ValueError("workload mode requires routed_component_limit")
        if self.mode == OptimizationMode.GENERAL and self.search.routed_component_limit is not None:
            raise ValueError("general mode cannot declare routed_component_limit")
        if len(self.authorized_components) != len(set(self.authorized_components)):
            raise ValueError("authorized_components must be unique")
        return self

    def metric(self, metric_id: str) -> MetricContract:
        for metric in self.metrics:
            if metric.id == metric_id:
                return metric
        raise KeyError(metric_id)

    @property
    def primary_metric(self) -> MetricContract:
        return next(metric for metric in self.metrics if metric.role == MetricRole.BUSINESS_PRIMARY)


def parse_optimization_policy_yaml(content: str) -> SystemOptimizationPolicy:
    try:
        payload = yaml.safe_load(content)
    except yaml.YAMLError as error:
        raise PolicyError("optimization policy YAML is invalid") from error
    if not isinstance(payload, dict):
        raise PolicyError("optimization policy YAML must contain one object")
    try:
        return SystemOptimizationPolicy.model_validate(payload)
    except ValueError as error:
        raise PolicyError(str(error)) from error


__all__ = [
    "HardGateContract",
    "IdentityPolicy",
    "MetricContract",
    "MetricDirection",
    "MetricRole",
    "OPTIMIZATION_POLICY_SCHEMA",
    "OptimizationMode",
    "PolicyError",
    "PressureMethod",
    "SearchPolicy",
    "SafetyExecutionContract",
    "StatisticsPolicy",
    "SystemOptimizationPolicy",
    "parse_optimization_policy_yaml",
]
