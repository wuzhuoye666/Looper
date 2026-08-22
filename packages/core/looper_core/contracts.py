from __future__ import annotations

import math
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class Direction(StrEnum):
    MINIMIZE = "minimize"
    MAXIMIZE = "maximize"
    NONE = "none"


class Aggregation(StrEnum):
    MEAN = "mean"
    MEDIAN = "median"
    P50 = "p50"
    P95 = "p95"
    P99 = "p99"
    P999 = "p99.9"
    MAXIMUM = "maximum"
    CVAR99 = "cvar99"


class ExperimentMode(StrEnum):
    OPTIMIZATION = "optimization"
    SELECTION = "selection"


class Comparison(StrEnum):
    ABSOLUTE = "absolute"
    DIFFERENCE = "difference"
    RELATIVE = "relative"


class GateKind(StrEnum):
    EXECUTION = "execution"
    CORRECTNESS = "correctness"
    SAFETY = "safety"
    SLO = "slo"
    RESOURCE = "resource"
    STATISTICAL = "statistical"


class GateScope(StrEnum):
    ATTEMPT = "attempt"
    EVALUATION = "evaluation"
    CANDIDATE = "candidate"
    BLOCK = "block"
    TARGET = "target"
    PLACEMENT_PAIR = "placement_pair"
    STUDY = "study"


class Operator(StrEnum):
    EQ = "eq"
    NE = "ne"
    LT = "lt"
    LTE = "lte"
    GT = "gt"
    GTE = "gte"
    TRUE = "true"
    FALSE = "false"


class SearchParameter(StrictModel):
    type: Literal["integer", "number", "categorical", "boolean"]
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = Field(default=None, gt=0)
    log: bool = False
    choices: list[Any] | None = None
    default: Any | None = None
    when: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> SearchParameter:
        if self.type in {"integer", "number"}:
            if self.minimum is None or self.maximum is None:
                raise ValueError("numeric parameters require minimum and maximum")
            if self.minimum > self.maximum:
                raise ValueError("minimum cannot exceed maximum")
            if self.log and self.minimum <= 0:
                raise ValueError("log parameters require a positive minimum")
        elif self.type == "categorical":
            if not self.choices:
                raise ValueError("categorical parameters require choices")
        return self


class ObjectiveSpec(StrictModel):
    metric: str = Field(min_length=1)
    unit: str = Field(min_length=1, max_length=40)
    direction: Direction
    aggregation: Aggregation = Aggregation.MEDIAN
    comparison: Comparison = Comparison.RELATIVE
    epsilon: float = 0.0
    weight: float = Field(default=1.0, ge=0)
    minimum_samples: int = Field(default=3, ge=1)


class GateSpec(StrictModel):
    id: str = Field(min_length=1, max_length=100)
    kind: GateKind
    scope: GateScope = GateScope.CANDIDATE
    metric: str | None = None
    operator: Operator
    threshold: float | bool | None = None
    hard: bool = True
    message: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def validate_threshold(self) -> GateSpec:
        no_threshold = {Operator.TRUE, Operator.FALSE}
        if self.operator not in no_threshold and self.threshold is None:
            raise ValueError("comparison gates require a threshold")
        if self.metric is None and self.kind not in {GateKind.EXECUTION, GateKind.CORRECTNESS}:
            raise ValueError("this gate kind requires a metric")
        return self


class ExperimentalDesign(StrictModel):
    warmup_runs: int = Field(default=1, ge=0, le=100)
    min_repeats: int = Field(default=3, ge=1, le=1000)
    max_repeats: int = Field(default=3, ge=1, le=1000)
    max_retries: int = Field(default=1, ge=0, le=20)
    baseline_every_n: int = Field(default=4, ge=1, le=1000)
    cooldown_seconds: float = Field(default=0.0, ge=0, le=3600)
    confidence_level: float = Field(default=0.95, gt=0.5, lt=1)
    bootstrap_resamples: int = Field(default=1000, ge=100, le=100000)
    tail_min_samples: int = Field(default=100, ge=20, le=1000000)
    random_seed: int = Field(default=20260301, ge=0)
    cache_mode: Literal["cold", "warm", "declared"] = "declared"

    @model_validator(mode="after")
    def validate_repeat_range(self) -> ExperimentalDesign:
        if self.min_repeats > self.max_repeats:
            raise ValueError("min_repeats cannot exceed max_repeats")
        return self


class BudgetSpec(StrictModel):
    max_candidates: int = Field(default=12, ge=1, le=100000)
    max_attempts: int = Field(default=100, ge=1, le=1000000)
    wall_time_seconds: int = Field(default=3600, ge=1, le=31536000)


class OptimizerSpec(StrictModel):
    type: Literal["grid", "random", "optuna-tpe", "optuna-nsga2"] = "random"
    seed: int = Field(default=20260301, ge=0)


class ScenarioRoleSpec(StrictModel):
    id: str = Field(min_length=1, max_length=80, pattern=r"^[a-z][a-z0-9._-]*$")
    kind: Literal["target", "load-generator", "database", "service", "observer"]
    count: int = Field(default=1, ge=1, le=1024)
    co_located_with: str | None = None
    included_in_score: bool = True
    description: str = Field(default="", max_length=500)


class GoodputPolicy(StrictModel):
    metric: str = Field(min_length=1)
    unit: str = Field(min_length=1, max_length=40)
    committed_outcome: str = Field(default="committed", min_length=1, max_length=80)
    excluded_outcomes: list[
        Literal["abort", "deadlock", "rollback", "retry", "timeout", "error"]
    ] = Field(
        default_factory=lambda: [
            "abort",
            "deadlock",
            "rollback",
            "retry",
            "timeout",
            "error",
        ]
    )
    maximum_error_ratio: float = Field(default=0.001, ge=0, le=1)
    maximum_abort_ratio: float = Field(default=0.01, ge=0, le=1)
    maximum_timeout_ratio: float = Field(default=0.001, ge=0, le=1)

    @field_validator("excluded_outcomes")
    @classmethod
    def unique_outcomes(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("excluded outcomes must be unique")
        return values


class TailEvidenceSpec(StrictModel):
    metric: str = Field(min_length=1)
    unit: str = Field(min_length=1, max_length=40)
    minimum_samples: int = Field(default=100000, ge=100)
    required_statistics: list[Literal["p50", "p95", "p99", "p99.9", "maximum", "timeout"]] = Field(
        default_factory=lambda: ["p50", "p95", "p99", "p99.9", "maximum", "timeout"]
    )
    histogram_format: Literal["hdr", "raw", "upstream-summary"] = "hdr"
    timeout_accounting: Literal["separate", "deadline-latency"] = "separate"

    @field_validator("required_statistics")
    @classmethod
    def unique_statistics(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("tail statistics must be unique")
        return values


class LoadSearchSpec(StrictModel):
    type: Literal["bracketed-binary"] = "bracketed-binary"
    offered_load_metric: str = Field(min_length=1)
    unit: str = Field(min_length=1, max_length=40)
    calibration_repeats: int = Field(default=3, ge=2, le=20)
    common_load_fractions: list[float] = Field(default_factory=lambda: [0.5, 0.75, 1.0])
    initial_repeats: int = Field(default=3, ge=2, le=20)
    boundary_repeats: int = Field(default=5, ge=3, le=50)
    required_passes: int = Field(default=4, ge=2, le=50)
    resolution_ratio: float = Field(default=0.025, gt=0, le=0.25)
    minimum_effect_ratio: float = Field(default=0.05, gt=0, le=1)
    maximum_adaptive_points: int = Field(default=5, ge=1, le=30)
    expansion_factor: float = Field(default=1.25, gt=1, le=4)

    @model_validator(mode="after")
    def validate_search(self) -> LoadSearchSpec:
        if not self.common_load_fractions or any(
            value <= 0 for value in self.common_load_fractions
        ):
            raise ValueError("common load fractions must be positive")
        if len(self.common_load_fractions) != len(set(self.common_load_fractions)):
            raise ValueError("common load fractions must be unique")
        if self.boundary_repeats < self.initial_repeats:
            raise ValueError("boundary repeats cannot be below initial repeats")
        if self.required_passes > self.boundary_repeats:
            raise ValueError("required passes cannot exceed boundary repeats")
        return self


class ClientLoadAccounting(StrictModel):
    schema_version: Literal["v1alpha1"] = Field(alias="schemaVersion")
    planned_offered_tps: float = Field(alias="plannedOfferedTps", gt=0)
    measurement_seconds: float = Field(alias="measurementSeconds", gt=0)
    offered_requests: int = Field(alias="offeredRequests", ge=1)
    started_requests: int = Field(alias="startedRequests", ge=1)
    completed_requests: int = Field(alias="completedRequests", ge=0)
    timeout_requests: int = Field(alias="timeoutRequests", ge=0)
    rate_limiter_lag_ratio: float = Field(alias="rateLimiterLagRatio", ge=0, le=1)
    client_headroom_ratio: float = Field(alias="clientHeadroomRatio", ge=0, le=1)

    @field_validator("planned_offered_tps", "measurement_seconds")
    @classmethod
    def finite_positive_values(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("load accounting rates and durations must be finite")
        return value

    @model_validator(mode="after")
    def validate_request_chain(self) -> ClientLoadAccounting:
        if self.started_requests > self.offered_requests:
            raise ValueError("started requests cannot exceed offered requests")
        if self.completed_requests + self.timeout_requests != self.started_requests:
            raise ValueError(
                "completed and timeout requests must account for every started request"
            )
        return self


class ScenarioBenchmarkSpec(StrictModel):
    id: str = Field(min_length=1, max_length=100, pattern=r"^[a-z][a-z0-9._-]*$")
    name: str = Field(min_length=1, max_length=160)
    decision_question: str = Field(min_length=1, max_length=1000)
    user_value: str = Field(min_length=1, max_length=1000)
    workload_class: str = Field(min_length=1, max_length=120)
    topology: Literal["single-node", "client-server", "multi-node", "closed-loop"]
    roles: list[ScenarioRoleSpec] = Field(min_length=1)
    primary_metric: str = Field(min_length=1)
    slo_gates: list[GateSpec] = Field(default_factory=list)
    goodput: GoodputPolicy | None = None
    tail_evidence: TailEvidenceSpec | None = None
    load_search: LoadSearchSpec | None = None

    @model_validator(mode="after")
    def validate_roles(self) -> ScenarioBenchmarkSpec:
        role_ids = [role.id for role in self.roles]
        if len(role_ids) != len(set(role_ids)):
            raise ValueError("scenario role ids must be unique")
        if not any(role.kind == "target" for role in self.roles):
            raise ValueError("scenario requires a target role")
        return self


class PriceSnapshot(StrictModel):
    provider: str = Field(min_length=1, max_length=60)
    region: str = Field(min_length=1, max_length=80)
    currency: str = Field(min_length=3, max_length=8)
    hourly_amount: Decimal = Field(gt=0)
    quoted_at: str = Field(min_length=1, max_length=80)
    quote_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class TargetBindingSpec(StrictModel):
    target_id: str = Field(min_length=1, max_length=100)
    variant_id: str = Field(min_length=1, max_length=100)
    label: str = Field(min_length=1, max_length=160)
    placement_pair_id: str = Field(min_length=1, max_length=120)
    price: PriceSnapshot | None = None


class SelectionDesign(StrictModel):
    target_bindings: list[TargetBindingSpec] = Field(min_length=1)
    reference_offered_load: float | None = Field(default=None, gt=0)
    load_generator_target_id: str | None = Field(default=None, min_length=1, max_length=100)
    order_scheme: Literal["abba-baab", "balanced-random", "simultaneous"] = "balanced-random"
    inference_unit: Literal["time_block", "placement_pair"] = "time_block"
    minimum_placement_pairs: int = Field(default=1, ge=1, le=1000)
    random_seed: int = Field(default=20260301, ge=0)

    @model_validator(mode="after")
    def validate_bindings(self) -> SelectionDesign:
        target_ids = [binding.target_id for binding in self.target_bindings]
        if len(target_ids) != len(set(target_ids)):
            raise ValueError("selection target bindings must be unique")
        return self


class FrontierBlockEvidence(StrictModel):
    block_id: str = Field(min_length=1, max_length=160)
    time_block_id: str = Field(min_length=1, max_length=160)
    committed_goodput: float = Field(ge=0)
    latency_p99: float = Field(ge=0)
    latency_samples: int = Field(ge=0)
    error_ratio: float = Field(ge=0, le=1)
    abort_ratio: float = Field(ge=0, le=1)
    timeout_ratio: float = Field(ge=0, le=1)
    offered_load_achieved_ratio: float = Field(ge=0)
    rate_limiter_lag_ratio: float = Field(ge=0, le=1)
    client_headroom_ratio: float = Field(ge=0)
    correctness_passed: bool
    resource_valid: bool


class FrontierPointEvidence(StrictModel):
    offered_load: float = Field(gt=0)
    blocks: list[FrontierBlockEvidence] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_blocks(self) -> FrontierPointEvidence:
        block_ids = [block.block_id for block in self.blocks]
        if len(block_ids) != len(set(block_ids)):
            raise ValueError("frontier block ids must be unique")
        return self


class ExperimentSpec(StrictModel):
    mode: ExperimentMode = ExperimentMode.OPTIMIZATION
    benchmark_id: str = Field(min_length=1)
    benchmark_version: str = Field(min_length=1)
    target_ids: list[str] = Field(default_factory=lambda: ["local"])
    workload_ids: list[str] = Field(default_factory=list)
    baseline_parameters: dict[str, Any] = Field(default_factory=dict)
    search_space: dict[str, SearchParameter] = Field(default_factory=dict)
    objectives: list[ObjectiveSpec] = Field(default_factory=list)
    gates: list[GateSpec] = Field(default_factory=list)
    design: ExperimentalDesign = Field(default_factory=ExperimentalDesign)
    budget: BudgetSpec = Field(default_factory=BudgetSpec)
    optimizer: OptimizerSpec = Field(default_factory=OptimizerSpec)
    scenario: ScenarioBenchmarkSpec | None = None
    selection: SelectionDesign | None = None

    @field_validator("target_ids", "workload_ids")
    @classmethod
    def unique_ids(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("ids must be unique")
        return values

    @model_validator(mode="after")
    def validate_mode(self) -> ExperimentSpec:
        if not self.target_ids:
            raise ValueError("an experiment requires at least one target")
        if not self.objectives:
            raise ValueError("an experiment requires at least one objective")
        if self.mode == ExperimentMode.OPTIMIZATION:
            if self.scenario is not None or self.selection is not None:
                raise ValueError("optimization experiments cannot declare selection fields")
            return self
        if self.scenario is None or self.selection is None:
            raise ValueError("selection studies require scenario and selection contracts")
        if self.search_space:
            raise ValueError("selection studies compare targets and cannot declare a search space")
        if self.baseline_parameters:
            raise ValueError("selection studies cannot declare baseline tuning parameters")
        bound_targets = {binding.target_id for binding in self.selection.target_bindings}
        if bound_targets != set(self.target_ids):
            raise ValueError("selection target bindings must exactly match target_ids")
        if self.scenario.primary_metric not in {objective.metric for objective in self.objectives}:
            raise ValueError("scenario primary metric must be declared as an objective")
        return self


class ExperimentCreate(StrictModel):
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=2000)
    project_id: str = "default"
    spec: ExperimentSpec


class MetricObservation(StrictModel):
    schema_version: Literal["v1alpha1"] = Field(alias="schemaVersion")
    metric: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]*$")
    value: float | bool
    unit: str = Field(min_length=1, max_length=40)
    phase: Literal["warmup", "measurement", "validation", "cleanup"]
    workload: str | None = None
    sample_index: int | None = Field(default=None, alias="sampleIndex", ge=0)
    sample_count: int | None = Field(default=None, alias="sampleCount", ge=1)
    statistic: Literal[
        "sample",
        "mean",
        "median",
        "p50",
        "p95",
        "p99",
        "p99.9",
        "maximum",
        "rate",
        "cvar99",
        "count",
        "boolean",
    ] = "sample"
    timestamp: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)

    @field_validator("value")
    @classmethod
    def finite_value(cls, value: float | bool) -> float | bool:
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("metric values must be finite")
        return value


class ResultCheck(StrictModel):
    id: str
    passed: bool
    scope: Literal[
        "attempt", "evaluation", "candidate", "block", "target", "placement_pair", "study"
    ]
    kind: Literal["execution", "correctness", "safety", "slo", "resource", "statistical"]
    message: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class ResultArtifact(StrictModel):
    path: str
    role: Literal[
        "log", "trace", "result", "raw-result", "profile", "dataset", "histogram", "other"
    ]
    media_type: str = Field(alias="mediaType")
    description: str | None = None


class AttemptResult(StrictModel):
    schema_version: Literal["v1alpha1"] = Field(alias="schemaVersion")
    status: Literal["succeeded", "failed", "timed_out", "cancelled"]
    message: str | None = None
    checks: list[ResultCheck] = Field(default_factory=list)
    artifacts: list[ResultArtifact] = Field(default_factory=list)
    extensions: dict[str, Any] = Field(default_factory=dict)
