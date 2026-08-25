"""S8 结果向量与 S9 晋升合同（L8 排名与 L6c 退化触发的数据底座）。

架构层：L8/L6（docs/system-optimizer/architecture/overall.md）；公式来源
formula-provenance S8/S9 与 F-PROJECT-004。

Open decision 边界（不得静默定值）：原始指标 → U_i 的**归一化方式未确认**。
本模块只接受已归一化的效用值（六个维度全部"越高越好"），并用
``normalization_digest`` 绑定任务提供的归一化策略；任何权重、阈值、决胜
顺序都必须是显式任务输入。
"""

from __future__ import annotations

from collections.abc import Sequence
from math import isfinite
from typing import Literal

from pydantic import Field, model_validator

from looper_core.canonical import canonical_digest
from looper_core.contracts import StrictModel

GENERAL_RESULT_SCHEMA = "looper.general-result-vector/v1alpha1"
DIMENSIONS = ("u_cpu", "u_memory", "u_storage", "u_network", "u_stability", "u_regression")


class GeneralResultVector(StrictModel):
    schema_version: Literal[GENERAL_RESULT_SCHEMA] = GENERAL_RESULT_SCHEMA
    candidate_id: str = Field(min_length=1, max_length=200)
    u_cpu: float
    u_memory: float
    u_storage: float
    u_network: float
    u_stability: float
    u_regression: float
    normalization_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_finite(self) -> GeneralResultVector:
        for name in DIMENSIONS:
            if not isfinite(getattr(self, name)):
                raise ValueError(f"{name} must be finite")
        return self

    def value(self, dimension: str) -> float:
        if dimension not in DIMENSIONS:
            raise ValueError(f"unknown dimension: {dimension}")
        return getattr(self, dimension)

    @property
    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json"))


def _dominates(left: GeneralResultVector, right: GeneralResultVector) -> bool:
    return all(
        left.value(name) >= right.value(name) for name in DIMENSIONS
    ) and any(left.value(name) > right.value(name) for name in DIMENSIONS)


def pareto_layers(vectors: Sequence[GeneralResultVector]) -> list[int]:
    """Pareto layer per vector (0 = non-dominated best); deterministic peeling."""

    if not vectors:
        raise ValueError("pareto_layers requires at least one vector")
    remaining = list(range(len(vectors)))
    layers = [0] * len(vectors)
    layer = 0
    while remaining:
        front = [
            index
            for index in remaining
            if not any(
                _dominates(vectors[other], vectors[index])
                for other in remaining
                if other != index
            )
        ]
        for index in front:
            layers[index] = layer
        remaining = [index for index in remaining if index not in front]
        layer += 1
    return layers


def rank_vectors(
    vectors: Sequence[GeneralResultVector],
    tie_break_order: Sequence[str],
) -> list[int]:
    """Order vector indices by Pareto layer, then the explicit task tie-break."""

    if sorted(tie_break_order) != sorted(DIMENSIONS):
        raise ValueError(
            f"tie_break_order must cover exactly the six dimensions: {DIMENSIONS}"
        )
    layers = pareto_layers(vectors)
    return sorted(
        range(len(vectors)),
        key=lambda index: (
            layers[index],
            *[-vectors[index].value(name) for name in tie_break_order],
            index,
        ),
    )


class PromotionContract(StrictModel):
    """S9 promotion thresholds; every number is a task input, no defaults hidden."""

    min_observations: int = Field(ge=1)
    min_distinct_time_blocks: int = Field(ge=1)
    min_environments: int = Field(ge=1)


class VerificationObservation(StrictModel):
    candidate_id: str = Field(min_length=1, max_length=200)
    passed: bool
    time_block_id: str = Field(min_length=1, max_length=160)
    environment_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    evidence_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class PromotionEvidence(StrictModel):
    schema_version: str = "looper.promotion-evidence/v1alpha1"
    candidate_id: str = Field(min_length=1)
    promoted: bool
    reason: str = Field(min_length=1, max_length=1000)
    observation_count: int = Field(ge=0)
    distinct_time_blocks: int = Field(ge=0)
    distinct_environments: int = Field(ge=0)
    failed_observations: list[str] = Field(default_factory=list)

    @property
    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json"))


def evaluate_promotion(
    observations: Sequence[VerificationObservation],
    contract: PromotionContract,
) -> PromotionEvidence:
    """S9: best-observed -> validated only after the task-declared re-verification."""

    if not observations:
        raise ValueError("evaluate_promotion requires at least one observation")
    candidate_ids = {observation.candidate_id for observation in observations}
    if len(candidate_ids) != 1:
        raise ValueError(f"observations mix candidates: {sorted(candidate_ids)}")
    candidate_id = next(iter(candidate_ids))
    failures = [
        observation.evidence_digest
        for observation in observations
        if not observation.passed
    ]
    distinct_blocks = len({observation.time_block_id for observation in observations})
    distinct_environments = len(
        {observation.environment_digest for observation in observations}
    )
    if failures:
        return PromotionEvidence(
            candidate_id=candidate_id,
            promoted=False,
            reason=f"{len(failures)} re-verification observation(s) failed; "
            "promotion is fail-closed",
            observation_count=len(observations),
            distinct_time_blocks=distinct_blocks,
            distinct_environments=distinct_environments,
            failed_observations=failures,
        )
    if len(observations) < contract.min_observations:
        return PromotionEvidence(
            candidate_id=candidate_id,
            promoted=False,
            reason=f"observation count {len(observations)} below required "
            f"{contract.min_observations}",
            observation_count=len(observations),
            distinct_time_blocks=distinct_blocks,
            distinct_environments=distinct_environments,
        )
    if distinct_blocks < contract.min_distinct_time_blocks:
        return PromotionEvidence(
            candidate_id=candidate_id,
            promoted=False,
            reason=f"distinct time blocks {distinct_blocks} below required "
            f"{contract.min_distinct_time_blocks}",
            observation_count=len(observations),
            distinct_time_blocks=distinct_blocks,
            distinct_environments=distinct_environments,
        )
    if distinct_environments < contract.min_environments:
        return PromotionEvidence(
            candidate_id=candidate_id,
            promoted=False,
            reason=f"distinct environments {distinct_environments} below required "
            f"{contract.min_environments}",
            observation_count=len(observations),
            distinct_time_blocks=distinct_blocks,
            distinct_environments=distinct_environments,
        )
    return PromotionEvidence(
        candidate_id=candidate_id,
        promoted=True,
        reason="all re-verification observations passed and every declared "
        "threshold is met",
        observation_count=len(observations),
        distinct_time_blocks=distinct_blocks,
        distinct_environments=distinct_environments,
    )


def regression_triggered(
    vector: GeneralResultVector,
    *,
    threshold: float,
) -> bool:
    """L6c trigger: the regression dimension fell below the task-declared floor."""

    if not isfinite(threshold):
        raise ValueError("threshold must be finite")
    return vector.u_regression < threshold


__all__ = [
    "DIMENSIONS",
    "GeneralResultVector",
    "PromotionContract",
    "PromotionEvidence",
    "VerificationObservation",
    "evaluate_promotion",
    "pareto_layers",
    "rank_vectors",
    "regression_triggered",
]
